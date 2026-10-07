"""Workspace creation via official CLIs: real isolated PostgreSQL, fake CLI binaries, no network."""
import json
import os
import sys
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_im_postgres import database
from test_platform_auth import configure, im_run
from app import im, platform_auth as pa, platform_bridge as bridge, platform_workspace as workspace
from app.models import Run, Audit, PlatformConnection, Conversation, Message, User, now

# Placeholders for fake binaries only; never read from real configuration.
MOCK_TENANT = '-'.join(['tenant', 'placeholder'])
MOCK_USER = '-'.join(['user', 'placeholder'])
MOCK_APP_VALUE = '-'.join(['app', 'value', 'placeholder'])

FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
data = sys.stdin.read()
keys = [k for k in os.environ]
with open({log!r}, 'a') as f:
    f.write(json.dumps({{'args': args, 'stdin': data, 'env': dict(os.environ), 'home': os.environ.get('HOME')}}) + '\n')
mode = open({mode!r}).read()
def ok(payload):
    print(json.dumps(payload)); sys.exit(0)
def fail(message):
    print(json.dumps({{'ok': False, 'success': False, 'error': {{'type': 'api', 'message': message}}}}), file=sys.stderr); sys.exit(1)
joined = ' '.join(args)
HELP = {{
    'drive +update-title': ('Risk: write', ['url', 'token', 'type', 'title', 'output', 'as', 'format']),
    'drive +delete': ('Risk: high-risk-write', ['file-token', 'type', 'yes', 'as', 'format']),
    'drive +member-add': ('Risk: high-risk-write', ['token', 'type', 'member-id', 'perm', 'yes', 'as', 'format']),
    'drive +download': ('Risk: read', ['file-token', 'output', 'as']),
    'base +record-list': ('Risk: read', ['base-token', 'table-id', 'as', 'format']),
    'doc +fetch': ('Safety: effect=read  risk=low  confirmation=not_required', ['node', 'client-secret', 'format']),
    'doc block delete': ('Safety: effect=write  risk=high  confirmation=user_required', ['node', 'block-id', 'yes', 'format']),
}}
if '--help' in args:
    key = ' '.join(a for a in args if a != '--help')
    prog = 'dws' if args[0] in ('doc', 'sheet', 'aitable') else 'lark-cli'
    if key not in HELP:
        # Like cobra: an unknown subcommand prints the parent's help with exit code 0.
        print(args[0] + ' commands\nUsage:\n  ' + prog + ' ' + args[0] + ' [flags]'); sys.exit(0)
    risk, flags = HELP[key]
    lines = [key + ' help', 'Usage:', '  ' + prog + ' ' + key + ' [flags]', 'Flags:']
    for flag in flags:
        lines.append('      --' + flag + ' string   ' + ('output format: json (default) | pretty' if flag == 'format' else 'value'))
    print('\n'.join(lines + [risk])); sys.exit(0)
if 'validation' in mode and '+update-title' in joined:
    secret = os.environ.get('LARKSUITE_CLI_USER_ACCESS_TOKEN', '')
    print(json.dumps({{'ok': False, 'error': {{'type': 'validation', 'message': '--title is invalid for ' + secret}}}}), file=sys.stderr); sys.exit(1)
if args[:2] == ['drive', '+update-title']:
    ok({{'ok': True, 'data': {{'title': 'renamed'}}}})
if args[:2] == ['doc', '+fetch'] or args[:3] == ['doc', 'block', 'delete']:
    ok({{'success': True, 'data': {{'content': '钉钉正文'}}}})
if 'permission' in mode and ('+create' in joined or 'workbook-create' in joined):
    fail('permission denied: scope required')
if args[:2] == ['docs', '+create']:
    ok({{'ok': True, 'data': {{'document': {{'document_id': 'DOC12345', 'url': 'https://evil.example/x' if 'evil' in mode else 'https://my.feishu.cn/docx/DOC12345'}}}}}})
if args[:2] == ['sheets', '+workbook-create']:
    ok({{'ok': True, 'data': {{'spreadsheet': {{'spreadsheet_token': 'SHT12345', 'url': 'https://my.feishu.cn/sheets/SHT12345'}}}}}})
if args[:2] == ['base', '+base-create']:
    ok({{'ok': True, 'data': {{'base': {{'base_token': 'BAS12345', 'url': 'https://my.feishu.cn/base/BAS12345'}}}}}})
if args[:3] == ['drive', 'permission.members', 'transfer_owner']:
    fail('transfer refused') if 'transfer_fail' in mode or 'share_fail' in mode else ok({{'ok': True, 'data': {{}}}})
if args[:2] == ['drive', '+member-add']:
    fail('member refused') if 'share_fail' in mode else ok({{'ok': True, 'data': {{}}}})
if args[:2] == ['drive', '+delete']:
    ok({{'ok': True, 'data': {{'deleted': True}}}})
if args[:2] == ['auth', 'login']:
    ok({{'success': True}})
if args[:2] == ['doc', '+create']:
    ok({{'success': True, 'data': {{'dentryUuid': 'DD123456', 'url': 'https://alidocs.dingtalk.com/i/nodes/DD123456'}}}})
if args[:2] == ['sheet', 'create-with-data'] or args[:2] == ['sheet', 'create']:
    ok({{'success': True, 'data': {{'workbookId': 'WB123456', 'url': 'https://alidocs.dingtalk.com/i/nodes/WB123456'}}}})
if 'notfound' in mode:
    fail('document not found')
if args[:2] == ['docs', '+fetch']:
    ok({{'ok': True, 'data': {{'document': {{'content': 'x' * 70000 if 'huge' in mode else '# 周报\n本周完成 X'}}}}}})
if args[:2] in (['docs', '+update'], ['sheets', '+cells-set'], ['base', '+record-batch-create']):
    ok({{'ok': True, 'data': {{}}}})
if args[:2] == ['sheets', '+workbook-info']:
    ok({{'ok': True, 'data': {{'sheets': [{{'sheet_id': 'sheet0001', 'title': 'Sheet1'}}]}}}})
if args[:2] == ['sheets', '+cells-get']:
    ok({{'ok': True, 'data': {{'values': [['姓名', '分数']]}}}})
if args[:2] in (['base', '+table-list'], ['base', '+record-list']) and 'json' not in args:
    # The real CLI prints Markdown tables unless JSON is requested.
    print('| markdown | table |'); sys.exit(0)
if args[:2] == ['base', '+table-list']:
    ok({{'ok': True, 'data': {{'tables': [{{'id': 'tblFirst0001', 'name': '任务'}}]}}}})
if args[:2] == ['base', '+record-list']:
    ok({{'ok': True, 'data': {{'records': [{{'标题': '第一条'}}]}}}})
if args[:2] in (['doc', 'read'], ['sheet', 'range']):
    ok({{'success': True, 'data': {{'content': '钉钉内容'}}}})
if args[:2] in (['doc', 'update'],):
    ok({{'success': True, 'data': {{}}}})
if args[:2] == ['sheet', 'list']:
    ok({{'success': True, 'data': {{'sheets': [{{'sheetId': 'dsheet0001'}}]}}}})
fail('unexpected command')
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    log, mode = tmp_path / 'calls.jsonl', tmp_path / 'mode'
    mode.write_text('ok')
    path = tmp_path / 'fake-cli'
    path.write_text(FAKE.format(python=sys.executable, log=str(log), mode=str(mode)))
    path.chmod(0o700)
    monkeypatch.setenv('PLATFORM_LARK_CLI', str(path))
    monkeypatch.setenv('PLATFORM_DWS_CLI', str(path))
    monkeypatch.setenv('FEISHU_APP_SECRET', MOCK_APP_VALUE)
    monkeypatch.setattr(im, 'access_token', lambda client, provider: MOCK_TENANT)
    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return mode, calls


def client_for(database):
    from fastapi.testclient import TestClient
    from app import main
    def session():
        # Mirrors get_db: a plain session committed at the end of the request.
        with database() as db:
            yield db
            db.commit()
    main.app.dependency_overrides[main.get_db] = session
    return TestClient(main.app), main


def token_for(database, group=False):
    run_id, user_id = im_run(database, group=group)
    with database.begin() as db:
        return bridge.issue(db.get(Run, run_id)), run_id, user_id


def invoke(client, token, name, args):
    response = client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
    return response


def body(response):
    assert response.status_code == 200, response.text
    return json.loads(response.json()['result']['content'][0]['text'])


def test_tools_listed_for_members(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        listed = client.post('/internal/platform-mcp', headers={'Authorization': 'Bearer ' + token},
                             json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).json()['result']['tools']
        names = {t['name']: t for t in listed}
        assert {'create_platform_document', 'create_platform_spreadsheet', 'create_platform_base'} <= set(names)
        assert names['create_platform_base']['inputSchema']['properties']['provider']['enum'] == ['feishu']
    finally:
        main.app.dependency_overrides.clear()


def test_feishu_document_runs_cli_as_bot_and_transfers_to_requester(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, run_id, user_id = token_for(database, group=True)
    client, main = client_for(database)
    try:
        result = body(invoke(client, token, 'create_platform_document',
                             {'provider': 'feishu', 'title': '周报', 'content': '# 本周\n- 完成 X'}))
    finally:
        main.app.dependency_overrides.clear()
    assert result['state'] == 'created' and result['url'] == 'https://my.feishu.cn/docx/DOC12345'
    assert result['handover'] == 'transferred_to_requester'
    create, transfer = calls()
    assert create['args'][:6] == ['docs', '+create', '--as', 'bot', '--title', '周报'] and create['stdin'] == '# 本周\n- 完成 X'
    env = create['env']
    assert env['LARKSUITE_CLI_TENANT_ACCESS_TOKEN'] == MOCK_TENANT and env['LARKSUITE_CLI_CONFIG_DIR'].startswith(create['home'])
    assert MOCK_APP_VALUE not in json.dumps(calls()) and 'FEISHU_APP_SECRET' not in env and 'DATABASE_URL' not in env
    assert not os.path.exists(create['home'])
    assert transfer['args'][:3] == ['drive', 'permission.members', 'transfer_owner']
    assert json.loads(transfer['args'][transfer['args'].index('--data') + 1]) == {'member_type': 'openid', 'member_id': 'sender'}
    with database() as db:
        audit = db.scalar(select(Audit).where(Audit.action == 'platform.workspace.create'))
        assert audit.details['resource'] == 'DOC12345' and '周报' not in json.dumps(audit.details, ensure_ascii=False)


def test_feishu_spreadsheet_and_base_arguments(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        sheet = body(invoke(client, token, 'create_platform_spreadsheet',
                            {'provider': 'feishu', 'title': '成绩', 'values': [['姓名', '分数'], ['张三', 95]]}))
        base = body(invoke(client, token, 'create_platform_base',
                           {'provider': 'feishu', 'title': '任务', 'table_name': '待办', 'columns': ['标题', '负责人']}))
    finally:
        main.app.dependency_overrides.clear()
    assert sheet['url'].endswith('/sheets/SHT12345') and base['url'].endswith('/base/BAS12345')
    sheet_call = calls()[0]
    assert '--values' in sheet_call['args'] and json.loads(sheet_call['stdin']) == [['姓名', '分数'], ['张三', 95]]
    base_call = calls()[2]
    fields = json.loads(base_call['args'][base_call['args'].index('--fields') + 1])
    assert fields == [{'name': '标题', 'type': 'text'}, {'name': '负责人', 'type': 'text'}]
    transfer_types = [c['args'][c['args'].index('--type') + 1] for c in calls() if 'transfer_owner' in c['args']]
    assert transfer_types == ['sheet', 'bitable']


def test_transfer_falls_back_to_full_access_then_deletes_when_unsharable(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    mode, calls = fake_cli
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        mode.write_text('transfer_fail')
        result = body(invoke(client, token, 'create_platform_document', {'provider': 'feishu', 'title': 'A'}))
        assert result['handover'] == 'requester_full_access'
        mode.write_text('share_fail')
        before = len(calls())
        result = body(invoke(client, token, 'create_platform_document', {'provider': 'feishu', 'title': 'B'}))
        assert result['state'] == 'share_failed' and 'url' not in result
        assert calls()[-1]['args'][:2] == ['drive', '+delete'] and len(calls()) - before == 4
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('mode,state', [('permission', 'platform_permission_missing'), ('evil', 'platform_call_failed')])
def test_platform_errors_and_foreign_links_never_reach_model(database, monkeypatch, fake_cli, mode, state):
    configure(monkeypatch)
    fake_cli[0].write_text(mode)
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        response = invoke(client, token, 'create_platform_document', {'provider': 'feishu', 'title': 'A'})
    finally:
        main.app.dependency_overrides.clear()
    result = body(response)
    assert result['state'] == state and 'url' not in result
    assert 'evil.example' not in response.text and 'scope required' not in response.text
    assert response.json()['result']['isError'] is True


def test_dingtalk_requires_personal_authorization_then_runs_dws_as_user(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, run_id, user_id = token_for(database)
    client, main = client_for(database)
    try:
        result = body(invoke(client, token, 'create_platform_document', {'provider': 'dingtalk', 'title': '纪要'}))
        assert result['state'] == 'authorization_required' and result['next_action'] == 'request_platform_authorization'
        assert calls() == []
        with database.begin() as db:
            db.add(PlatformConnection(user_id=user_id, provider='dingtalk', state='connected',
                                      encrypted=pa.seal({'access_token': MOCK_USER}), expires_at=now() + timedelta(minutes=30)))
        doc = body(invoke(client, token, 'create_platform_document', {'provider': 'dingtalk', 'title': '纪要', 'content': '正文'}))
        sheet = body(invoke(client, token, 'create_platform_spreadsheet', {'provider': 'dingtalk', 'title': '表', 'values': [['a']]}))
        refused = invoke(client, token, 'create_platform_base', {'provider': 'dingtalk', 'title': 'x'})
    finally:
        main.app.dependency_overrides.clear()
    assert doc['url'] == 'https://alidocs.dingtalk.com/i/nodes/DD123456' and doc['handover'] == 'owner'
    assert sheet['url'].endswith('/WB123456') and refused.status_code == 400
    login, create = calls()[0], calls()[1]
    assert login['args'][:4] == ['auth', 'login', '--token', MOCK_USER] and login['home'] == create['home']
    assert create['env']['DWS_DISABLE_KEYCHAIN'] == '1' and create['env']['DWS_CONFIG_DIR'].startswith(create['home'])
    assert create['stdin'] == '正文' and MOCK_USER not in json.dumps(doc)


@pytest.mark.parametrize('args', [
    {'provider': 'feishu', 'title': ''}, {'provider': 'feishu', 'title': 'x' * 201},
    {'provider': 'feishu', 'title': 'a\nb'}, {'provider': 'feishu', 'title': 'a', 'content': 'x' * 100001}])
def test_invalid_arguments_never_invoke_cli(database, monkeypatch, fake_cli, args):
    configure(monkeypatch)
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        response = invoke(client, token, 'create_platform_document', args)
    finally:
        main.app.dependency_overrides.clear()
    assert response.status_code == 413 or body(response)['state'] == 'invalid_arguments'
    assert fake_cli[1]() == []


def test_unknown_argument_and_missing_identity(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        assert invoke(client, token, 'create_platform_document', {'provider': 'feishu', 'title': 'a', 'owner': 'x'}).status_code == 400
    finally:
        main.app.dependency_overrides.clear()
    # A web run by a user without a bound Feishu identity cannot receive the document.
    with database.begin() as db:
        user = User(email='web@example.invalid', name='web', role='member', org_id='org', team_id='team', active=True, password_hash='unused')
        db.add(user); db.flush()
        c = Conversation(owner_id=user.id, title='web'); db.add(c); db.flush()
        m = Message(conversation_id=c.id, role='user', content='建文档'); db.add(m); db.flush()
        r = Run(user_id=user.id, conversation_id=c.id, message_id=m.id, status='running'); db.add(r); db.flush()
        web_token = bridge.issue(r)
    client, main = client_for(database)
    try:
        result = body(invoke(client, web_token, 'create_platform_document', {'provider': 'feishu', 'title': 'a'}))
    finally:
        main.app.dependency_overrides.clear()
    assert result['state'] == 'identity_missing' and fake_cli[1]() == []


def test_missing_cli_reports_install_problem(database, monkeypatch, fake_cli, tmp_path):
    configure(monkeypatch)
    monkeypatch.setenv('PLATFORM_LARK_CLI', str(tmp_path / 'absent'))
    token, _, _ = token_for(database)
    client, main = client_for(database)
    try:
        assert body(invoke(client, token, 'create_platform_document', {'provider': 'feishu', 'title': 'a'}))['state'] == 'cli_not_installed'
    finally:
        main.app.dependency_overrides.clear()


DOC_URL = 'https://my.feishu.cn/docx/DOC12345'
OTHER_DOC = 'https://my.feishu.cn/docx/OTHER0001'


def connect(database, user_id, provider='feishu', scope='docx:document sheets:spreadsheet'):
    with database.begin() as db:
        sealed = {'access_token': MOCK_USER, **({'scope': scope} if scope else {})}
        db.merge(PlatformConnection(user_id=user_id, provider=provider, state='connected',
                                    encrypted=pa.seal(sealed), expires_at=now() + timedelta(minutes=30)))


def run_tools(database, token, *calls):
    client, main = client_for(database)
    try:
        return [invoke(client, token, name, args) for name, args in calls]
    finally:
        main.app.dependency_overrides.clear()


def approve(database, run_id, user_id, code, verb='approve'):
    """The requester's own /approve (or /deny) message, as the ingress would apply it."""
    from app import approvals
    from app.models import Conversation
    with database.begin() as db:
        run, user = db.get(Run, run_id), db.get(User, user_id)
        return approvals.handle(db, user, db.get(Conversation, run.conversation_id), approvals.Decision(verb, code))


def run_approved(database, token, code):
    return body(run_tools(database, token, ('run_approved_platform_action', {'approval_id': code}))[0])


def test_group_reads_and_writes_only_hub_created_resources_via_bot(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=True)
    connect(database, user_id)  # Personal identity is never used for group read/write.
    created, read, write, other = run_tools(database, token,
        ('create_platform_document', {'provider': 'feishu', 'title': '周报'}),
        ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': DOC_URL + '?from=im'}),
        ('write_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': DOC_URL, 'content': '追加'}),
        ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))
    assert body(created)['identity'] == 'user'  # Creation as the requester is harmless anywhere.
    read_result, write_result = body(read), body(write)
    assert body(other)['state'] == 'private_chat_required'
    assert read_result['state'] == 'read' and read_result['identity'] == 'bot' and '本周完成' in json.dumps(read_result, ensure_ascii=False)
    assert write_result['state'] == 'written' and write_result['identity'] == 'bot'
    fetch, update = [c for c in calls() if c['args'][:2] in (['docs', '+fetch'], ['docs', '+update'])]
    assert fetch['args'][fetch['args'].index('--doc') + 1] == DOC_URL and '--as' in fetch['args'] and 'bot' in fetch['args']
    assert update['args'][update['args'].index('--command') + 1] == 'append' and update['stdin'] == '追加'
    assert 'LARKSUITE_CLI_USER_ACCESS_TOKEN' not in update['env']
    with database() as db:
        audits = list(db.scalars(select(Audit).where(Audit.action.in_(['platform.workspace.read', 'platform.workspace.write']))))
        assert len(audits) == 2 and '本周完成' not in json.dumps([a.details for a in audits], ensure_ascii=False)


def test_private_personal_identity_reads_any_link_and_creates_without_transfer(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=False)
    unauthorized = body(run_tools(database, token, ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))[0])
    assert unauthorized['state'] == 'authorization_required' and calls() == []
    connect(database, user_id)
    created, read = run_tools(database, token, ('create_platform_document', {'provider': 'feishu', 'title': '周报'}),
                              ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))
    assert body(created)['handover'] == 'owner' and body(read)['identity'] == 'user'
    assert not any('transfer_owner' in c['args'] for c in calls())
    for call in calls():
        assert call['env']['LARKSUITE_CLI_USER_ACCESS_TOKEN'] == MOCK_USER and 'LARKSUITE_CLI_TENANT_ACCESS_TOKEN' not in call['env']
        assert call['args'][call['args'].index('--as') + 1] == 'user'
    assert MOCK_USER not in created.text + read.text


def test_token_authorized_before_scope_expansion_is_not_used(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id, scope=None)
    created, read = run_tools(database, token, ('create_platform_document', {'provider': 'feishu', 'title': '周报'}),
                              ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))
    assert body(created)['handover'] == 'transferred_to_requester'
    assert body(read)['state'] == 'authorization_required'


def test_spreadsheet_and_base_read_write_arguments(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    sheet = 'https://my.feishu.cn/sheets/SHEET0001?sheet=abc'
    base = 'https://my.feishu.cn/base/BASE00001?table=x'
    results = run_tools(database, token,
        ('read_platform_resource', {'provider': 'feishu', 'kind': 'spreadsheet', 'url': sheet}),
        ('write_platform_resource', {'provider': 'feishu', 'kind': 'spreadsheet', 'url': sheet, 'anchor': 'B2',
                                     'values': [['张三', 95, True], ['李四']]}),
        ('read_platform_resource', {'provider': 'feishu', 'kind': 'base', 'url': base, 'table': '任务'}),
        ('write_platform_resource', {'provider': 'feishu', 'kind': 'base', 'url': base, 'records': [{'标题': 'A', '标签': ['x']}]}))
    assert [body(r)['state'] for r in results] == ['read', 'written', 'read', 'written']
    assert body(results[1])['written'] == 'B2:D3'
    by_command = {tuple(c['args'][:2]): c['args'] for c in calls()}
    get = by_command[('sheets', '+cells-get')]
    assert get[get.index('--sheet-id') + 1] == 'sheet0001' and get[get.index('--range') + 1] == 'A1:Z200'
    assert get[get.index('--url') + 1] == 'https://my.feishu.cn/sheets/SHEET0001'
    cells = json.loads(by_command[('sheets', '+cells-set')][by_command[('sheets', '+cells-set')].index('--cells') + 1])
    assert cells == [[{'value': '张三'}, {'value': 95}, {'value': True}], [{'value': '李四'}, {'value': ''}, {'value': ''}]]
    record_list = by_command[('base', '+record-list')]
    assert record_list[record_list.index('--table-id') + 1] == '任务' and record_list[record_list.index('--base-token') + 1] == 'BASE00001'
    create = by_command[('base', '+record-batch-create')]
    assert create[create.index('--table-id') + 1] == 'tblFirst0001'  # Defaults to the first table.
    assert json.loads(create[create.index('--json') + 1]) == {'create_records': [{'标题': 'A', '标签': ['x']}]}


@pytest.mark.parametrize('args,state', [
    ({'kind': 'document', 'url': 'https://evil.example/docx/DOC12345'}, 'invalid_url'),
    ({'kind': 'document', 'url': 'http://my.feishu.cn/docx/DOC12345'}, 'invalid_url'),
    ({'kind': 'spreadsheet', 'url': DOC_URL}, 'kind_mismatch'),
    ({'kind': 'document', 'url': 'https://my.feishu.cn/drive/folder/FOLDER0001'}, 'invalid_url'),
    ({'kind': 'spreadsheet', 'url': 'https://my.feishu.cn/sheets/SHEET0001', 'values': [['a']], 'anchor': 'A0'}, 'invalid_arguments'),
    ({'kind': 'document', 'url': DOC_URL, 'content': 'x', 'mode': 'delete'}, 'invalid_arguments'),
    ({'kind': 'base', 'url': 'https://my.feishu.cn/base/BASE00001', 'records': [{'a': {'nested': 1}}]}, 'invalid_arguments'),
])
def test_invalid_links_and_write_arguments_never_invoke_cli(database, monkeypatch, fake_cli, args, state):
    configure(monkeypatch)
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    name = 'write_platform_resource' if any(k in args for k in ('values', 'records', 'content')) else 'read_platform_resource'
    response = run_tools(database, token, (name, {'provider': 'feishu', **args}))[0]
    assert body(response)['state'] == state and fake_cli[1]() == []


def test_large_read_is_truncated_and_missing_resource_reported(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    mode, _ = fake_cli
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    mode.write_text('huge')
    huge = body(run_tools(database, token, ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))[0])
    assert huge['truncated'] is True and len(huge['data']) == bridge.READ_LIMIT
    mode.write_text('notfound')
    missing = body(run_tools(database, token, ('read_platform_resource', {'provider': 'feishu', 'kind': 'document', 'url': OTHER_DOC}))[0])
    assert missing['state'] == 'resource_not_found' and missing['next_action'] == 'check_link'


def test_dingtalk_read_write_private_only(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    node = 'https://alidocs.dingtalk.com/i/nodes/NODE00001'
    group_token, _, user_id = token_for(database, group=True)
    connect(database, user_id, provider='dingtalk', scope=None)
    assert body(run_tools(database, group_token, ('read_platform_resource', {'provider': 'dingtalk', 'kind': 'document', 'url': node}))[0])['state'] == 'private_chat_required'
    token, run_id, private_user = token_for(database, group=False)
    connect(database, private_user, provider='dingtalk', scope=None)
    read, doc, sheet = run_tools(database, token,
        ('read_platform_resource', {'provider': 'dingtalk', 'kind': 'document', 'url': node}),
        ('write_platform_resource', {'provider': 'dingtalk', 'kind': 'document', 'url': node, 'content': '覆盖', 'mode': 'overwrite'}),
        ('write_platform_resource', {'provider': 'dingtalk', 'kind': 'spreadsheet', 'url': node, 'values': [['a', 1, False]]}))
    # Replacing a whole document waits for the user's own approval; ordinary reads and writes do not.
    assert [body(r)['state'] for r in (read, doc, sheet)] == ['read', 'approval_required', 'written']
    approve(database, run_id, private_user, body(doc)['approval_code'])
    assert run_approved(database, token, body(doc)['approval_code'])['state'] == 'written'
    commands = [c['args'] for c in calls() if c['args'][:2] != ['auth', 'login']]
    assert commands[0][:3] == ['doc', 'read', '--node'] and commands[-1][-1] == '--yes' and commands[-1][:2] == ['doc', 'update']
    update = next(c for c in commands if c[:3] == ['sheet', 'range', 'update'])
    assert update[update.index('--sheet-id') + 1] == 'dsheet0001' and update[update.index('--range') + 1] == 'A1:C1'
    assert json.loads(update[update.index('--values') + 1]) == [[{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': '1'}, {'type': 'text', 'text': 'false'}]]


def executed(calls):
    return [c['args'] for c in calls() if '--help' not in c['args'] and c['args'][:2] != ['auth', 'login']]


def test_describe_then_rename_title_as_requester(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    domains, described, renamed = run_tools(database, token,
        ('describe_platform_command', {'provider': 'feishu'}),
        ('describe_platform_command', {'provider': 'feishu', 'command': ['drive', '+update-title']}),
        ('run_platform_command', {'provider': 'feishu', 'command': ['drive', '+update-title'],
                                  'flags': {'url': OTHER_DOC, 'title': '测试文档1004'}}))
    assert body(domains)['state'] == 'described' and 'docs:' in body(domains)['text'] and 'mail' not in body(domains)['text']
    assert body(described)['state'] == 'described' and '--title' in body(described)['text']
    result = body(renamed)
    assert result['state'] == 'done' and result['data'] == {'title': 'renamed'} and result['identity'] == 'user'
    [args] = executed(calls)
    assert args[:2] == ['drive', '+update-title'] and f'--url={OTHER_DOC}' in args and '--title=测试文档1004' in args
    assert args[-4:] == ['--as', 'user', '--format', 'json']
    with database() as db:
        audit = db.scalar(select(Audit).where(Audit.action == 'platform.workspace.command'))
        assert audit.details['command'] == 'drive +update-title' and audit.details['flags'] == ['title', 'url']
        assert '测试文档1004' not in json.dumps(audit.details, ensure_ascii=False)
        # Describe is local help only and leaves no audit trail.
        assert db.scalar(select(Audit).where(Audit.action == 'platform.workspace.describe')) is None


@pytest.mark.parametrize('command,flags,stdin,state', [
    (['mail', '+send'], {}, None, 'command_not_allowed'),
    (['auth', 'login'], {}, None, 'command_not_allowed'),
    (['drive', '+download'], {}, None, 'local_file_not_allowed'),
    (['drive', '+update-title'], {'output': 'x.txt'}, None, 'local_file_not_allowed'),
    (['drive', '+update-title'], {'title': '@/etc/passwd'}, None, 'local_file_not_allowed'),
    (['drive', '+update-title'], {'title': '-'}, None, 'local_file_not_allowed'),
    (['drive', '+update-title'], {'as': 'bot'}, None, 'flag_not_allowed'),
    (['drive', '+update-title'], {'format': 'pretty'}, None, 'flag_not_allowed'),
    (['drive', '+update-title'], {'nope': 1}, None, 'unknown_flag'),
    (['drive', '+update-title'], {'title': {'nested': 1}}, None, 'invalid_arguments'),
    (['docs', '+nope'], {}, None, 'unknown_command'),
    (['docs', '--help'], {}, None, 'invalid_arguments'),
])
def test_command_policy_never_runs_rejected_calls(database, monkeypatch, fake_cli, command, flags, stdin, state):
    configure(monkeypatch)
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    args = {'provider': 'feishu', 'command': command, 'flags': flags, **({'stdin': stdin} if stdin is not None else {})}
    assert body(run_tools(database, token, ('run_platform_command', args))[0])['state'] == state
    assert executed(fake_cli[1]) == []


def test_stdin_feeds_dash_flag(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    result = body(run_tools(database, token, ('run_platform_command', {'provider': 'feishu', 'command': ['drive', '+update-title'],
                                                                       'flags': {'url': OTHER_DOC, 'title': '-'}, 'stdin': '长标题'}))[0])
    assert result['state'] == 'done'
    call = next(c for c in calls() if c['args'][:2] == ['drive', '+update-title'] and '--help' not in c['args'])
    assert '--title=-' in call['args'] and call['stdin'] == '长标题'


def test_high_risk_command_runs_only_after_the_user_approves(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, run_id, user_id = token_for(database, group=False)
    connect(database, user_id)
    request = {'provider': 'feishu', 'command': ['drive', '+delete'], 'flags': {'file-token': 'OTHER0001', 'type': 'docx'}}
    pending = body(run_tools(database, token, ('run_platform_command', request))[0])
    assert pending['state'] == 'approval_required' and pending['next_action'] == 'wait_for_user_approval'
    code = pending['approval_code']
    assert 'drive +delete' in pending['summary'] and 'OTHER0001' in pending['summary'] and 'payload' not in json.dumps(pending)
    assert executed(calls) == []
    # The model asserting anything cannot release it; only the user's own decision can.
    assert run_approved(database, token, code)['state'] == 'approval_pending'
    assert run_approved(database, token, 'ZZZZZZ')['state'] == 'approval_not_found'
    assert executed(calls) == []
    assert approve(database, run_id, user_id, code).continuation.startswith(f'我批准了操作 {code}')
    done = run_approved(database, token, code)
    assert done['state'] == 'done' and done['identity'] == 'user'
    [args] = executed(calls)
    assert '--yes' in args and '--file-token=OTHER0001' in args
    assert run_approved(database, token, code)['state'] == 'approval_used'
    assert len(executed(calls)) == 1
    with database() as db:
        executed_audit = db.scalar(select(Audit).where(Audit.action == 'platform.workspace.command'))
        assert executed_audit.details['high_risk'] is True and executed_audit.details['approval'] == code
        actions = {a.action for a in db.scalars(select(Audit))}
        assert {'platform.approval.requested', 'platform.approval.approved', 'platform.approval.executed'} <= actions


def test_group_command_is_pinned_to_hub_created_resource_via_bot(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, _, user_id = token_for(database, group=True)
    connect(database, user_id)
    rename = {'provider': 'feishu', 'command': ['drive', '+update-title'], 'flags': {'title': '新标题'}}
    results = run_tools(database, token,
        ('run_platform_command', rename),
        ('create_platform_document', {'provider': 'feishu', 'title': '周报'}),
        ('run_platform_command', {**rename, 'target_url': DOC_URL}),
        ('run_platform_command', {**rename, 'target_url': OTHER_DOC}),
        ('run_platform_command', {**rename, 'target_url': DOC_URL, 'flags': {'title': 'x', 'url': OTHER_DOC}}),
        ('run_platform_command', {**rename, 'target_url': DOC_URL, 'flags': {'title': 'x', 'token': 'OTHER0001'}}),
        ('run_platform_command', {'provider': 'feishu', 'command': ['drive', '+member-add'], 'target_url': DOC_URL,
                                  'flags': {'member-id': 'ou_other'}}))
    states = [body(r)['state'] for r in results]
    assert states == ['private_chat_required', 'created', 'done', 'private_chat_required', 'flag_not_allowed',
                      'flag_not_allowed', 'private_chat_required']
    assert body(results[2])['identity'] == 'bot'
    renames = [a for a in executed(calls) if a[:2] == ['drive', '+update-title']]
    assert len(renames) == 1 and f'--url={DOC_URL}' in renames[0] and renames[0][-4:] == ['--as', 'bot', '--format', 'json']
    assert not any(a[:2] == ['drive', '+member-add'] for a in executed(calls))


def test_private_without_authorization_only_reaches_hub_created(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    token, _, _ = token_for(database, group=False)
    rename = {'provider': 'feishu', 'command': ['drive', '+update-title'], 'flags': {'title': 'x'}}
    first, other = run_tools(database, token, ('run_platform_command', rename),
                             ('run_platform_command', {**rename, 'target_url': OTHER_DOC}))
    assert body(first)['state'] == body(other)['state'] == 'authorization_required'
    assert executed(fake_cli[1]) == []


def test_command_error_detail_is_returned_without_credentials(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    mode, _ = fake_cli
    mode.write_text('validation')
    token, _, user_id = token_for(database, group=False)
    connect(database, user_id)
    response = run_tools(database, token, ('run_platform_command', {'provider': 'feishu', 'command': ['drive', '+update-title'],
                                                                    'flags': {'url': OTHER_DOC, 'title': 'x'}}))[0]
    result = body(response)
    assert result['state'] == 'platform_call_failed' and 'validation' in result['error_detail']
    assert '--title is invalid for ***' in result['error_detail'] and MOCK_USER not in response.text


def test_dingtalk_command_private_only_with_confirmation(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    node = 'https://alidocs.dingtalk.com/i/nodes/NODE00001'
    group_token, _, user_id = token_for(database, group=True)
    connect(database, user_id, provider='dingtalk', scope=None)
    fetch = {'provider': 'dingtalk', 'command': ['doc', '+fetch'], 'flags': {'node': node}}
    assert body(run_tools(database, group_token, ('run_platform_command', fetch))[0])['state'] == 'private_chat_required'
    token, run_id, private_user = token_for(database, group=False)
    connect(database, private_user, provider='dingtalk', scope=None)
    delete = {'provider': 'dingtalk', 'command': ['doc', 'block', 'delete'], 'flags': {'node': node, 'block-id': 'b1'}}
    results = run_tools(database, token,
        ('run_platform_command', fetch),
        ('run_platform_command', {**fetch, 'flags': {'node': node, 'client-secret': 'x'}}),
        ('run_platform_command', delete),
        ('run_platform_command', {'provider': 'dingtalk', 'command': ['doc', '+media-insert'], 'flags': {}}))
    assert [body(r)['state'] for r in results] == ['done', 'flag_not_allowed', 'approval_required', 'local_file_not_allowed']
    code = body(results[2])['approval_code']
    approve(database, run_id, private_user, code)
    assert run_approved(database, token, code)['state'] == 'done'
    fetched, deleted = executed(calls)
    assert fetched[:2] == ['doc', '+fetch'] and fetched[-2:] == ['-f', 'json'] and '--as' not in fetched
    assert '--yes' in deleted
