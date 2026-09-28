import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from test_im_postgres import database
from app import platform_auth as pa, platform_bridge as bridge
from app.models import User, PlatformConnection, Conversation, Message, Run, now


def configure(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'test-platform-key-12345678901234567890')
    monkeypatch.setenv('PLATFORM_BRIDGE_KEY', 'test-bridge-key-12345678901234567890')
    for p in pa.PROVIDERS:
        monkeypatch.setenv('PLATFORM_' + p.upper() + '_CLIENT_ID', 'client')
        monkeypatch.setenv('PLATFORM_' + p.upper() + '_CLIENT_SECRET', 'secret')


def device(provider):
    import hashlib
    return {'device_code': 'secret-device', 'user_code': 'secret-code', 'url': 'https://accounts.feishu.cn/authorize?code=secret',
            'expires_in': 240, 'interval': 5, 'client_fingerprint': hashlib.sha256(b'client\0secret').hexdigest()}


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
        assert pa.operate(db, u, 'feishu', 'start')['state'] == 'setup_required'
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


def test_protocol_feishu(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'device_code':'D','user_code':'U','verification_uri':'https://accounts.feishu.cn/device','expires_in':240,'interval':5})
    d = pa.begin('feishu')
    assert d['device_code'] == 'D'
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'error':'authorization_pending'})
    assert pa.poll('feishu', d)[0] == 'authorization_pending'
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'access_token':'TOKEN','scope':'docx:document:readonly','expires_in':700})
    assert pa.poll('feishu', d)[0] == 'connected'
    monkeypatch.setattr(pa, 'request', lambda *a, **k: {'access_token':'TOKEN','scope':'im:message:send_as_user'})
    assert pa.poll('feishu', d)[0] == 'setup_required'


def test_protocol_dingtalk_organization_gate(monkeypatch):
    configure(monkeypatch)
    responses = iter([{'success':True,'result':{'deviceCode':'D','userCode':'U','verificationUri':'https://login.dingtalk.com/device','expiresIn':900,'interval':5,'flowId':'F'}},
                      {'success':True,'data':{'status':'APPROVED','authCode':'A'}},
                      {'accessToken':'TOKEN','expiresIn':7200}, {'success':True,'result':{'cliAuthEnabled':False}}])
    calls = []
    def respond(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)
    monkeypatch.setattr(pa, 'request', respond)
    d = pa.begin('dingtalk')
    assert pa.poll('dingtalk', d) == ('setup_required', {})
    assert calls[1][1] == 'https://mcp.dingtalk.com/cli/oauth/device/poll'
    assert calls[2][2]['json']['grantType'] == 'authorization_code'
