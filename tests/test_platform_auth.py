import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from test_im_postgres import database
from app import platform_auth as pa, platform_bridge as bridge
from app.models import User, PlatformConnection, PlatformSettings, Audit, Conversation, Message, Run, now


def configure(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'test-platform-key-12345678901234567890')
    monkeypatch.setenv('PLATFORM_BRIDGE_KEY', 'test-bridge-key-12345678901234567890')
    for p in pa.PROVIDERS:
        monkeypatch.setenv('PLATFORM_' + p.upper() + '_CLIENT_ID', 'app')
        monkeypatch.setenv('PLATFORM_' + p.upper() + '_CLIENT_SECRET', 'secret')


def device(provider):
    import hashlib
    return {'device_code': 'secret-device', 'user_code': 'secret-code', 'url': 'https://accounts.feishu.cn/authorize?code=secret',
            'expires_in': 240, 'interval': 5, 'client_fingerprint': hashlib.sha256(b'app\0secret').hexdigest()}


def test_isolation_idempotency_expiry_cancel(database, monkeypatch):
    configure(monkeypatch)
    calls = []
    monkeypatch.setattr(pa, 'begin', lambda p: calls.append(p) or device(p))
    with database.begin() as db:
        user = db.scalar(select(User)); uid = user.id
        second = User(email='two@example.invalid', name='two', role='member', password_hash='unused', active=True)
        db.add(second); db.flush(); second_id = second.id
    def start(i):
        with database.begin() as db:
            return pa.operate(db, db.get(User, uid), 'feishu', 'start', private=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        out = list(pool.map(start, range(2)))
    assert len(calls) == 0 and all(r['state'] == 'starting' for r in out)
    from app.platform_worker import tick
    tick(database)
    assert len(calls) == 1
    with database.begin() as db:
        assert pa.operate(db, db.get(User, second_id), 'feishu')['state'] == 'disconnected'
        public = pa.operate(db, db.get(User, uid), 'feishu')
        assert 'secret' not in json.dumps(public)
        row = db.get(PlatformConnection, (uid, 'feishu'))
        assert 'secret-device' not in row.encrypted
        row.expires_at = now() - timedelta(seconds=1)
    with database.begin() as db:
        assert pa.operate(db, db.get(User, uid), 'feishu')['state'] == 'expired'
        assert pa.operate(db, db.get(User, uid), 'feishu', 'start')['state'] == 'starting'
        assert pa.operate(db, db.get(User, uid), 'feishu', 'cancel')['state'] == 'disconnected'
        assert db.get(PlatformConnection, (uid, 'feishu')).encrypted == ''


def test_setup_and_redaction(database, monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_SECRET')
    with database.begin() as db:
        u = db.scalar(select(User))
        assert pa.operate(db, u, 'feishu', 'start')['state'] == 'configuration_missing'
    monkeypatch.setenv('PLATFORM_FEISHU_CLIENT_SECRET', 'secret')
    monkeypatch.setattr(pa, 'begin', lambda p: (_ for _ in ()).throw(ValueError('LEAK-ME')))
    with database.begin() as db:
        result = pa.operate(db, db.scalar(select(User)), 'feishu', 'start')
        assert result['state'] == 'starting'
    from app.platform_worker import tick
    tick(database)
    with database.begin() as db:
        result = pa.operate(db, db.scalar(select(User)), 'feishu')
        assert result['state'] == 'platform_rejected' and 'LEAK-ME' not in json.dumps(result)


@pytest.mark.parametrize('url', ['http://accounts.feishu.cn/', 'https://accounts.feishu.cn.evil.test/', 'javascript:alert(1)', 'https://evil.test/', 'https://user@accounts.feishu.cn/', 'https://accounts.feishu.cn:123/'])
def test_link_allowlist(url):
    assert not pa.valid_link('feishu', url)


def test_capability_bound_to_run_user_conversation(database, monkeypatch):
    configure(monkeypatch)
    with database.begin() as db:
        u = db.scalar(select(User)); c = Conversation(owner_id=u.id, title='test'); db.add(c); db.flush()
        m = Message(conversation_id=c.id, role='user', content='test'); db.add(m); db.flush()
        r = Run(user_id=u.id, conversation_id=c.id, message_id=m.id, status='running'); db.add(r); db.flush()
        token = bridge.issue(r)
        assert bridge.verify(token, db).id == u.id
        with pytest.raises(HTTPException): bridge.verify(token[:-1] + ('0' if token[-1] != '0' else '1'), db)
        r.status = 'succeeded'
        with pytest.raises(HTTPException): bridge.verify(token, db)
        r.status = 'running'; u.active = False
        with pytest.raises(HTTPException): bridge.verify(token, db)


def test_http_owner_only_and_mcp_no_material(database, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main, security
    configure(monkeypatch)
    monkeypatch.setattr(pa, 'begin', device)
    with database.begin() as db:
        owner = db.scalar(select(User)); owner_id = owner.id
        c = Conversation(owner_id=owner.id, title='test'); db.add(c); db.flush()
        m = Message(conversation_id=c.id, role='user', content='test'); db.add(m); db.flush()
        r = Run(user_id=owner.id, conversation_id=c.id, message_id=m.id, status='running'); db.add(r); db.flush()
        token = bridge.issue(r)
    def session():
        with database.begin() as db: yield db
    def user():
        with database() as db: return db.get(User, owner_id)
    main.app.dependency_overrides[main.get_db] = session
    main.app.dependency_overrides[security.current_user] = user
    try:
        client = TestClient(main.app)
        result = client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token}, json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':bridge.TOOLS[1],'arguments':{'provider':'feishu'}}})
        assert result.status_code == 200 and 'secret-code' not in result.text and 'accounts.feishu.cn' not in result.text
        assert result.headers['cache-control'] == 'no-store'
        from app.platform_worker import tick
        tick(database)
        personal = client.get('/api/platform-connections')
        assert personal.status_code == 200 and 'secret-code' in personal.text
        assert client.get('/api/platform-connections/other-user').status_code in (404,405)
        bad = client.post('/internal/platform-mcp', headers={'Authorization':'Bearer '+token}, json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':bridge.TOOLS[1],'arguments':{'provider':'feishu','userId':'other'}}})
        assert bad.status_code == 400
        main.app.dependency_overrides.pop(security.current_user)
        assert client.get('/api/platform-connections').status_code == 401
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('action', ['cancel', 'disconnect'])
def test_personal_management_cannot_target_another_user_even_as_admin(database, monkeypatch, action):
    from fastapi.testclient import TestClient
    from app import main, security
    configure(monkeypatch)
    with database.begin() as db:
        owner = db.scalar(select(User)); owner_id = owner.id
        admin = User(email='admin@example.invalid',name='admin',role='super_admin',password_hash='unused',active=True)
        db.add(admin);db.flush();admin_id=admin.id
        row=PlatformConnection(user_id=owner_id,provider='feishu',state='connected',encrypted=pa.seal({'access_token':'OWNER-TOKEN'}),expires_at=now()+timedelta(minutes=10))
        db.add(row)
    def session():
        with database.begin() as db: yield db
    def actor():
        with database() as db: return db.get(User,admin_id)
    main.app.dependency_overrides[main.get_db]=session
    main.app.dependency_overrides[security.current_user]=actor
    try:
        client=TestClient(main.app)
        response=client.post('/api/platform-connections/feishu/'+action)
        assert response.status_code==200 and 'OWNER-TOKEN' not in response.text
        with database.begin() as db:
            assert db.get(PlatformConnection,(owner_id,'feishu')).state=='connected'
            assert pa.unseal(db.get(PlatformConnection,(owner_id,'feishu')))['access_token']=='OWNER-TOKEN'
        assert client.post('/api/platform-connections/feishu/'+action+'/'+owner_id).status_code in (404,405)
        main.app.dependency_overrides[security.current_user]=lambda: User(id=owner_id,active=True,role='member')
        response=client.post('/api/platform-connections/feishu/'+action)
        assert response.status_code==200 and response.json()['state']=='disconnected'
        with database.begin() as db:
            assert db.get(PlatformConnection,(owner_id,'feishu')).encrypted==''
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('origin,reachable', [
    ('https://hub.example.invalid', True),
    ('http://127.0.0.1:18200', False),
    ('https://localhost', False),
    ('https://hub.example.invalid/path', False),
    ('https://hub.example.invalid?code=private', False),
    ('https://hub.example.invalid/#private', False),
])
def test_model_entry_links_to_current_chat_without_private_material(database, monkeypatch, origin, reachable):
    import socket
    from fastapi.testclient import TestClient
    from app import main
    configure(monkeypatch)
    monkeypatch.setenv('APP_ORIGIN', origin)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: [(None,None,None,None,('127.0.0.1' if host=='localhost' else '8.8.8.8',443))])
    with database.begin() as db:
        owner = db.scalar(select(User)); c = Conversation(owner_id=owner.id, title='entry'); db.add(c); db.flush()
        m = Message(conversation_id=c.id, role='user', content='entry'); db.add(m); db.flush()
        r = Run(user_id=owner.id, conversation_id=c.id, message_id=m.id, status='running'); db.add(r); db.flush()
        token, conversation_id = bridge.issue(r), c.id
    def session():
        with database.begin() as db: yield db
    main.app.dependency_overrides[main.get_db] = session
    try:
        response = TestClient(main.app).post('/internal/platform-mcp', headers={'Authorization':'Bearer '+token}, json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':bridge.TOOLS[0],'arguments':{'provider':'feishu'}}})
        assert response.status_code == 200
        result = json.loads(response.json()['result']['content'][0]['text'])
        if reachable:
            assert result['personal_connection_page'] == origin + '/#/chat/' + conversation_id
        else:
            assert 'personal_connection_page' not in result
        assert 'hub_entry_message' not in result and result['next_action'] == 'request_authorization'
        assert '#/connections' not in response.text
        assert 'user_code' not in result and 'authorization_url' not in result
    finally:
        main.app.dependency_overrides.clear()


def test_protocol_feishu(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'device_code':'D','user_code':'U','verification_uri':'https://accounts.feishu.cn/device','expires_in':240,'interval':5})
    d = pa.begin('feishu')
    assert d['device_code'] == 'D'
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'error':'authorization_pending'})
    assert pa.poll('feishu', d)[0] == 'authorization_pending'
    granted = 'docx:document sheets:spreadsheet base:record:read offline_access'
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'access_token':'TOKEN','scope':granted,'expires_in':700})
    state, tokens = pa.poll('feishu', d)
    assert state == 'connected' and set(tokens['scope'].split()) == set(granted.split())
    # The device request asks for the whole document-domain set, never broader domains.
    calls = []
    monkeypatch.setattr(pa, 'request', lambda *a, **k: calls.append(k) or {'device_code':'D','user_code':'U','verification_uri':'https://accounts.feishu.cn/device','expires_in':240,'interval':5})
    pa.begin('feishu')
    requested = set(calls[0]['data']['scope'].split())
    assert {'docx:document', 'sheets:spreadsheet', 'base:record:create', 'base:record:delete', 'drive:drive',
            'docs:document.comment:create', 'wiki:node:create', 'space:document:move'} <= requested
    assert not any(s.startswith(('mail:', 'im:', 'calendar:', 'contact:', 'approval:', 'task:')) for s in requested)
    assert 'search:message' not in requested
    # Read-only grants cannot write documents, and tokens reaching into other domains are rejected outright.
    for scope, reason in (('docx:document:readonly', 'SCOPE_NOT_WRITABLE'), ('docx:document im:message:send_as_user', 'SCOPE_OUT_OF_DOMAIN:im'),
                          ('docx:document mail:user_mailbox:readonly', 'SCOPE_OUT_OF_DOMAIN:mail')):
        monkeypatch.setattr(pa, 'request', lambda *a, **k: {'access_token':'TOKEN','scope':scope})
        with pytest.raises(pa.ProviderError) as caught:
            pa.poll('feishu', d)
        assert (caught.value.state, caught.value.reason) == ('provider_invalid_config', reason)


def test_protocol_dingtalk_organization_gate(monkeypatch):
    configure(monkeypatch)
    responses = iter([{'success':True,'result':'dingofficial'},
                      {'success':True,'result':{'deviceCode':'D','userCode':'U','verificationUri':'https://login.dingtalk.com/device','expiresIn':900,'interval':5,'flowId':'F'}},
                      {'success':True,'data':{'status':'APPROVED','authCode':'A'}},
                      {'accessToken':'TOKEN','expiresIn':7200}, {'success':True,'result':{'cliAuthEnabled':False}}])
    calls = []
    def respond(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)
    monkeypatch.setattr(pa, 'request', respond)
    d = pa.begin('dingtalk')
    with pytest.raises(pa.ProviderError) as caught:
        pa.poll('dingtalk', d)
    # Being an administrator does not switch CLI data access on; the reason is kept so the user is told what to do.
    assert (caught.value.state, caught.value.reason) == ('organization_denied', 'CLI_NOT_ENABLED')
    assert calls[0][1] == 'https://mcp.dingtalk.com/cli/clientId'
    assert calls[1][2]['data']['client_id'] == 'dingofficial'
    assert calls[2][1] == 'https://mcp.dingtalk.com/cli/oauth/device/poll'
    # The code is exchanged through the MCP proxy with the official client, never with the app secret.
    assert calls[3][1] == 'https://mcp.dingtalk.com/oauth2/getToken'
    assert calls[3][2]['json'] == {'clientId': 'dingofficial', 'authCode': 'A', 'grantType': 'authorization_code'}



def test_protocol_dingtalk_keeps_the_approving_account(monkeypatch):
    """getToken names who approved; the worker compares it with the bound person instead of calling the open API."""
    configure(monkeypatch)
    responses = iter([{'success':True,'result':'dingofficial'},
                      {'success':True,'result':{'deviceCode':'D','userCode':'U','verificationUri':'https://login.dingtalk.com/device','expiresIn':900,'interval':5,'flowId':'F'}},
                      {'success':True,'data':{'status':'APPROVED','authCode':'A'}},
                      {'accessToken':'TOKEN','expiresIn':7200,'corpId':'ding-corp','corpName':'组织','userId':'staff-1','userName':'本人'},
                      {'success':True,'result':{'cliAuthEnabled':True}}])
    calls = []
    monkeypatch.setattr(pa, 'request', lambda method, url, **kwargs: calls.append(url) or next(responses))
    d = pa.begin('dingtalk')
    state, tokens = pa.poll('dingtalk', d)
    assert (state, tokens) == ('connected', {'access_token': 'TOKEN', 'expires_in': 7200, 'user_id': 'staff-1', 'corp_id': 'ding-corp'})
    assert not any('api.dingtalk.com' in url or 'oapi.dingtalk.com' in url for url in calls)

# --- Admin-only IM-triggered platform application configuration ---

def im_run(database, group=True, provider='feishu'):
    from app.im import _enqueue
    from app.models import uid as gen_uid
    if provider != 'feishu':
        from app.models import Group, Identity
        from app.im_discovery import pin, scope
        with database.begin() as db:
            owner = db.scalar(select(User))
            if not db.scalar(select(Identity).where(Identity.provider == provider)):
                identity = Identity(provider=provider, external_user_id='sender', user_id=owner.id)
                group = Group(name=provider + '群', org_id=owner.org_id, team_id=owner.team_id, member_ids=[owner.id], provider=provider, external_id='chat')
                db.add_all([identity, group])
                db.flush()
                pin(db, 'identity', identity.id, scope(provider))
                pin(db, 'group', group.id, scope(provider))
    with database.begin() as db:
        _enqueue(db, provider, gen_uid(), 'sender', 'chat' if group else 'private-chat', '管理员配置请求', group)
        run = db.scalar(select(Run).order_by(Run.created_at.desc()))
        run.status = 'running'
        run_id, user_id = run.id, run.user_id
    return run_id, user_id


def set_role(database, user_id, role):
    with database.begin() as db:
        db.get(User, user_id).role = role


def mcp_client(database, token):
    from fastapi.testclient import TestClient
    from app import main
    def session():
        with database.begin() as db:
            yield db
    main.app.dependency_overrides[main.get_db] = session
    return TestClient(main.app), main


def call(client, token, name, provider='feishu', extra=None):
    args = {'provider': provider, **(extra or {})}
    return client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token},
                        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})


def test_admin_tools_hidden_and_rejected_for_non_admin(database, monkeypatch):
    configure(monkeypatch)
    run_id, user_id = im_run(database, group=False)
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        listed = client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token},
                              json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        names = [t['name'] for t in listed.json()['result']['tools']]
        assert 'get_platform_application_status' not in names and 'configure_platform_application' not in names
        rejected = call(client, token, 'get_platform_application_status')
        assert rejected.status_code == 400
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('role', ['super_admin', 'org_admin'])
def test_admin_tools_visible_for_admins(database, monkeypatch, role):
    configure(monkeypatch)
    run_id, user_id = im_run(database, group=False)
    set_role(database, user_id, role)
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        listed = client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token},
                              json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        names = {t['name'] for t in listed.json()['result']['tools']}
        assert {'get_platform_application_status', 'configure_platform_application'} <= names
    finally:
        main.app.dependency_overrides.clear()


def test_admin_status_and_reuse_bot_app_via_im(database, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv('FEISHU_APP_SECRET', 'bot-secret-value')
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_ID', raising=False)
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_SECRET', raising=False)
    run_id, user_id = im_run(database, group=False)
    set_role(database, user_id, 'super_admin')
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        status = call(client, token, 'get_platform_application_status')
        assert status.status_code == 200
        payload = json.loads(status.json()['result']['content'][0]['text'])
        assert payload['configured'] is False and payload['bot_app']['available'] is True
        assert 'bot-secret-value' not in status.text
        configured = call(client, token, 'configure_platform_application')
        assert configured.status_code == 200
        result_payload = json.loads(configured.json()['result']['content'][0]['text'])
        assert result_payload['configured'] is True and result_payload['credential_source'] == 'bot_app_copy'
        assert 'bot-secret-value' not in configured.text
        with database.begin() as db:
            row = db.get(PlatformSettings, 'feishu')
            assert row is not None
            from app import im_settings
            stored = json.loads(im_settings.cipher().decrypt(row.encrypted.encode()))
            assert stored['CLIENT_SECRET'] == 'bot-secret-value'
            audits = list(db.scalars(select(Audit).where(Audit.action == 'platform.configuration.update_via_im')))
            assert len(audits) == 1 and audits[0].target_id == 'feishu'
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('role,expect_web_entry,expect_contact', [('super_admin', True, False), ('org_admin', False, True)])
def test_admin_not_reusable_entry_point_by_role(database, monkeypatch, role, expect_web_entry, expect_contact):
    import socket
    configure(monkeypatch)
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_ID', raising=False)
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_SECRET', raising=False)
    monkeypatch.setenv('APP_ORIGIN', 'https://hub.example.invalid')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: [(None, None, None, None, ('8.8.8.8', 443))])
    run_id, user_id = im_run(database, group=False)
    set_role(database, user_id, role)
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        resp = call(client, token, 'get_platform_application_status')
        payload = json.loads(resp.json()['result']['content'][0]['text'])
        assert payload['bot_app']['available'] is False
        assert ('web_entry' in payload) == expect_web_entry
        assert payload['contact_super_admin_required'] == expect_contact
    finally:
        main.app.dependency_overrides.clear()


def test_admin_group_trigger_requires_private_chat_and_skips_write(database, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv('FEISHU_APP_SECRET', 'bot-secret-value')
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_ID', raising=False)
    monkeypatch.delenv('PLATFORM_FEISHU_CLIENT_SECRET', raising=False)
    run_id, user_id = im_run(database, group=True)
    set_role(database, user_id, 'super_admin')
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        resp = call(client, token, 'configure_platform_application')
        assert resp.status_code == 200
        payload = json.loads(resp.json()['result']['content'][0]['text'])
        assert payload['admin_action_result'] == 'private_chat_required'
        assert 'bot_app' not in payload and 'web_entry' not in payload
        assert 'CLIENT_ID' not in resp.text and 'bot-secret-value' not in resp.text
        with database.begin() as db:
            assert db.get(PlatformSettings, 'feishu') is None
    finally:
        main.app.dependency_overrides.clear()


def test_admin_tool_rejects_extra_arguments(database, monkeypatch):
    configure(monkeypatch)
    run_id, user_id = im_run(database, group=False)
    set_role(database, user_id, 'super_admin')
    with database.begin() as db:
        token = bridge.issue(db.get(Run, run_id))
    client, main = mcp_client(database, token)
    try:
        resp = call(client, token, 'configure_platform_application', extra={'revision': 999, 'bot_snapshot': 'a' * 16})
        assert resp.status_code == 400
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('status,reason', [
    ({'success': True, 'result': {'cliAuthEnabled': True}}, None),
    ({'success': True, 'result': {'cliAuthEnabled': False}}, 'CLI_NOT_ENABLED'),
    ({'success': True, 'result': {'cliAuthEnabled': False, 'userScope': 'specified', 'allowedUsers': ['x']}}, 'CLI_USER_NOT_ALLOWED'),
    ({'success': True, 'result': {'cliAuthEnabled': False, 'userScope': 'forbidden'}}, 'CLI_USER_FORBIDDEN'),
    ({'success': True, 'result': {'cliAuthEnabled': False, 'userScope': 'specified', 'channelScope': 'specified'}}, 'CHANNEL_REQUIRED'),
    ({'success': False, 'errorCode': 'ENTERPRISE_NOT_AUTHORIZED', 'errorMsg': 'x'}, 'ENTERPRISE_NOT_AUTHORIZED'),
    ({'success': False}, 'CLI_STATUS_UNKNOWN'),
])
def test_dingtalk_cli_denial_reasons(status, reason):
    assert pa.cli_denial(status) == reason
    assert reason is None or reason in pa.DENIALS
