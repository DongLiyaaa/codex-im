"""Feishu nickname enrichment: real isolated PostgreSQL, mocked Feishu transport only."""
import json
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_im_postgres import database
from app import im, im_connections as connections, im_discovery as discovery, im_nicknames as nicknames
from app.models import IMDiscovery, IMChatName, Audit

ALICE, BOB, CAROL = 'ou_alice0001', 'ou_bob000002', 'ou_carol0003'
GROUP = 'oc_group0001'
# Placeholders for the mocked transport only; not real credentials.
MOCK_TOKEN = '-'.join(['tenant', 'mock'])
MOCK_APP_VALUE = '-'.join(['unit', 'test', 'only'])


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_SECRET', MOCK_APP_VALUE)
    im._token_cache.clear()
    nicknames._failures.clear()
    yield
    nicknames._failures.clear()


def root():
    return SimpleNamespace(id='root', role='super_admin', active=True, org_id=None)


def seed(database, rows):
    with database.begin() as db:
        for provider, sender, chat, is_group, nickname in rows:
            discovery.record(db, provider, discovery.scope(provider), sender, chat, is_group, 'unknown_sender', nickname)


def token_body():
    body = {'code': 0, 'expire': 7200}
    body['tenant_access_token'] = MOCK_TOKEN
    return body


def transport(routes, calls):
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200, json=token_body())
        assert request.headers['authorization'] == 'Bearer ' + MOCK_TOKEN
        status, body = routes(request)
        return httpx.Response(status, json=body)
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def run(database, factory):
    # Mirrors get_db: a plain session committed at the end of the request.
    with database() as db:
        result = nicknames.refresh(db, factory)
        db.commit()
    return result


def names(database):
    with database() as db:
        return {(r.sender_id, r.chat_type): r.nickname for r in db.scalars(select(IMDiscovery))}


def test_contact_lookup_group_fallback_and_cached_failure(database):
    seed(database, [('feishu', ALICE, 'oc_private01', False, None), ('feishu', BOB, GROUP, True, None),
                    ('feishu', CAROL, 'oc_private02', False, None)])
    def routes(request):
        path = request.url.path
        if path == '/open-apis/contact/v3/users/' + ALICE:
            assert request.url.params['user_id_type'] == 'open_id'
            return 200, {'code': 0, 'data': {'user': {'name': '张三\u0007', 'nickname': 'zs'}}}
        if path.startswith('/open-apis/contact/v3/users/'):
            return 400, {'code': 41050, 'msg': 'no user authority'}
        if path == '/open-apis/im/v1/chats/' + GROUP + '/members':
            assert request.url.params['member_id_type'] == 'open_id'
            return 200, {'code': 0, 'data': {'items': [{'member_id': BOB, 'name': '李四'}], 'has_more': False}}
        if path == '/open-apis/im/v1/chats/' + GROUP:
            return 200, {'code': 0, 'data': {'name': '产品群', 'chat_mode': 'group'}}
        raise AssertionError(path)
    calls = []
    result = run(database, transport(routes, calls))
    assert result['status'] == 'ok' and result['resolved'] == 2 and result['unresolved'] == 1
    assert result['chat_resolved'] == 1 and result['chat_unresolved'] == 0
    assert result['reasons'] == {'outside_contact_scope': 1}
    assert names(database) == {(ALICE, 'p2p'): '张三', (BOB, 'group'): '李四', (CAROL, 'p2p'): None}
    with database() as db:
        listed = {r['sender_id']: r['nickname_status'] for r in discovery.discoveries(root(), db)}
    assert listed == {ALICE: 'available', BOB: 'available', CAROL: 'outside_contact_scope'}
    # Failure is cached: a second refresh makes no outbound lookup for the same sender.
    again = []
    assert run(database, transport(routes, again))['cached'] == 1
    assert again == []


def test_permission_errors_and_bot_outside_group(database):
    seed(database, [('feishu', BOB, GROUP, True, None)])
    def routes(request):
        if '/contact/' in request.url.path:
            return 400, {'code': 99991672, 'msg': 'Access denied'}
        return 400, {'code': 232011, 'msg': 'Operator can NOT be out of the chat'}
    result = run(database, transport(routes, []))
    assert result['reasons'] == {'permission_missing': 1}
    assert result['chat_reasons'] == {'bot_not_in_chat': 1}
    assert names(database) == {(BOB, 'group'): None}


def test_member_pages_follow_page_token(database):
    seed(database, [('feishu', BOB, GROUP, True, None)])
    def routes(request):
        if '/contact/' in request.url.path:
            return 400, {'code': 41050}
        if 'page_token' not in request.url.params:
            return 200, {'code': 0, 'data': {'items': [{'member_id': ALICE, 'name': '张三'}], 'has_more': True, 'page_token': 'p2'}}
        assert request.url.params['page_token'] == 'p2'
        return 200, {'code': 0, 'data': {'items': [{'member_id': BOB, 'name': '李四'}], 'has_more': False}}
    assert run(database, transport(routes, []))['resolved'] == 1
    assert names(database) == {(BOB, 'group'): '李四'}


def test_existing_nickname_dingtalk_and_invalid_ids_untouched(database):
    seed(database, [('feishu', ALICE, 'oc_private01', False, '已有昵称'), ('dingtalk', 'staff1', 'cid', False, '钉钉昵称'),
                    ('dingtalk', 'staff2', 'cid2', False, None), ('feishu', 'sender', 'chat', True, None)])
    calls = []
    def unexpected(request):
        raise AssertionError(request.url)
    result = run(database, transport(unexpected, calls))
    assert result['reasons'] == {'invalid_id': 1}
    assert [c.url.path for c in calls] == ['/open-apis/auth/v3/tenant_access_token/internal']
    with database() as db:
        listed = {r['sender_id']: (r['nickname'], r['nickname_status']) for r in discovery.discoveries(root(), db)}
    assert listed['staff1'] == ('钉钉昵称', 'available') and listed['staff2'] == (None, 'not_provided')
    assert listed[ALICE] == ('已有昵称', 'available') and listed['sender'] == (None, 'invalid_id')


def test_application_change_during_lookup_discards_results(database, monkeypatch):
    seed(database, [('feishu', ALICE, 'oc_private01', False, None)])
    def routes(request):
        monkeypatch.setenv('FEISHU_APP_ID', 'rotated-app')
        return 200, {'code': 0, 'data': {'user': {'name': '张三'}}}
    assert run(database, transport(routes, []))['status'] == 'application_changed'
    assert names(database) == {(ALICE, 'p2p'): None}


def test_token_failure_unconfigured_and_busy(database, monkeypatch):
    seed(database, [('feishu', ALICE, 'oc_private01', False, None)])
    def broken():
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'code': 10014})))
    assert run(database, broken)['status'] == 'token_failed'
    assert nicknames._failures == {}
    monkeypatch.delenv('FEISHU_APP_SECRET')
    assert run(database, broken)['status'] == 'unconfigured'
    nicknames._refresh_lock.acquire()
    try:
        assert run(database, broken)['status'] == 'busy'
    finally:
        nicknames._refresh_lock.release()


def test_transient_failure_uses_short_retry(database):
    seed(database, [('feishu', ALICE, 'oc_private01', False, None)])
    result = run(database, transport(lambda r: (503, {'code': 1}), []))
    assert result['reasons'] == {'lookup_failed': 1}
    code, expiry = next(iter(nicknames._failures.values()))
    assert code == 'lookup_failed' and expiry - nicknames.time.monotonic() <= nicknames.TTL['lookup_failed']


def test_endpoint_admin_only_and_audit_has_no_names(database, monkeypatch):
    seed(database, [('feishu', ALICE, 'oc_private01', False, None)])
    monkeypatch.setattr(nicknames, '_client', transport(lambda r: (200, {'code': 0, 'data': {'user': {'name': '张三'}}}), []))
    with pytest.raises(HTTPException):
        discovery.admin(SimpleNamespace(id='m', role='org_admin', active=True))
    with database() as db:
        result = discovery.refresh_nicknames(root(), db)
        db.commit()
    assert result['resolved'] == 1
    with database() as db:
        audit = db.scalar(select(Audit).where(Audit.action == 'im.discovery.nickname_refresh'))
        assert audit.details == {'status': 'ok', 'resolved': 1, 'unresolved': 0, 'remaining': 0,
                                 'chat_resolved': 0, 'chat_unresolved': 0, 'chat_remaining': 0}
        assert '张三' not in json.dumps(audit.details, ensure_ascii=False)


def listed_chats(database):
    with database() as db:
        return {r['chat_id']: (r['chat_name'], r['chat_name_status']) for r in discovery.discoveries(root(), db)}


def test_feishu_group_name_i18n_fallback_and_pending_bind_prefill(database):
    seed(database, [('feishu', BOB, GROUP, True, None), ('feishu', ALICE, 'oc_private01', False, None)])
    def routes(request):
        if '/contact/' in request.url.path:
            return 200, {'code': 0, 'data': {'user': {'name': '成员'}}}
        assert request.url.path == '/open-apis/im/v1/chats/' + GROUP
        return 200, {'code': 0, 'data': {'name': '', 'i18n_names': {'zh_cn': '产品研发群'}}}
    result = run(database, transport(routes, []))
    assert result['chat_resolved'] == 1
    assert listed_chats(database) == {GROUP: ('产品研发群', 'available'), 'oc_private01': (None, 'private_chat')}
    with database() as db:
        assert [g['name'] for g in discovery.discovered_groups(root(), db)] == ['产品研发群']
    # Already named chats and resolved senders trigger no further outbound calls.
    again = []
    run(database, transport(routes, again))
    assert again == []


def test_feishu_group_name_failure_is_reported_and_cached(database):
    seed(database, [('feishu', BOB, GROUP, True, '已有昵称')])
    result = run(database, transport(lambda r: (400, {'code': 99991672}), []))
    assert result['chat_reasons'] == {'permission_missing': 1}
    assert listed_chats(database) == {GROUP: (None, 'permission_missing')}
    again = []
    assert run(database, transport(lambda r: (400, {'code': 99991672}), again))['chat_cached'] == 1
    assert again == []


def test_application_change_discards_group_names(database, monkeypatch):
    seed(database, [('feishu', BOB, GROUP, True, '已有昵称')])
    def routes(request):
        monkeypatch.setenv('FEISHU_APP_ID', 'rotated-app')
        return 200, {'code': 0, 'data': {'name': '产品群'}}
    assert run(database, transport(routes, []))['status'] == 'application_changed'
    with database() as db:
        assert db.scalar(select(IMChatName)) is None


def dingtalk(msg, title, kind='2', chat='cid-new'):
    return {'msgId': msg, 'msgtype': 'text', 'text': {'content': 'hello'}, 'senderStaffId': 'newstaff',
            'senderNick': '王五', 'conversationId': chat, 'conversationType': kind,
            'conversationTitle': title, 'robotCode': 'robot'}


def test_dingtalk_stream_title_stored_updated_and_ignored_for_private(database):
    assert connections.receive_dingtalk(dingtalk('m1', '研发群'), database)['pending']
    assert listed_chats(database) == {'cid-new': ('研发群', 'available')}
    connections.receive_dingtalk(dingtalk('m2', '研发一群'), database)
    connections.receive_dingtalk(dingtalk('m3', '王五', kind='1', chat='cid-private'), database)
    assert listed_chats(database) == {'cid-new': ('研发一群', 'available'), 'cid-private': (None, 'private_chat')}
    with database() as db:
        assert db.scalar(select(IMDiscovery).where(IMDiscovery.chat_id == 'cid-new')).nickname == '王五'
        assert [c.chat_id for c in db.scalars(select(IMChatName))] == ['cid-new']


def test_dingtalk_group_without_title_reports_not_provided(database):
    with database.begin() as db:
        assert im._enqueue(db, 'dingtalk', 'm1', 'staff9', 'cid-untitled', 'hi', True)['pending']
    assert listed_chats(database) == {'cid-untitled': (None, 'not_provided')}
