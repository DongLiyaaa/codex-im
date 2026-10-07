"""Isolated PostgreSQL + mock HTTP only; never touches historical platform messages."""
import httpx
import pytest
from sqlalchemy import select, func
from app import im, im_reactions as reactions
from app.models import IMReaction, IMEvent, Run, now
from datetime import timedelta
from test_im_postgres import database


@pytest.fixture
def setup(database, monkeypatch):
    monkeypatch.setenv('FEISHU_APP_SECRET', 'mock-only')
    with database.begin() as db:
        im._enqueue(db, 'feishu', 'om_mock_new', 'sender', 'chat', 'hello', True)
        run = db.scalar(select(Run))
        run.status = 'running'
        run_id = run.id
    calls = []
    client = httpx.Client
    def handler(request):
        calls.append((request.method, request.url.path))
        if 'auth/v3' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock', 'expire': 7200})
        return httpx.Response(200, json={'code': 0, 'data': {'reaction_id': 'mock-reaction'}})
    monkeypatch.setattr(reactions.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    im._token_cache.clear()
    return database, run_id, calls, client


def test_lifecycle_and_duplicate(setup):
    factory, rid, calls, _ = setup
    reactions.process(rid, True, factory)
    reactions.process(rid, True, factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'active'
        assert row.message_id == 'om_mock_new'
        assert db.scalar(select(IMEvent)).event_id != row.message_id
    reactions.process(rid, factory=factory)
    reactions.process(rid, factory=factory)
    assert [x[0] for x in calls if '/messages/' in x[1]] == ['POST', 'DELETE']
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleared' and row.message_id is None


@pytest.mark.parametrize('failure', ['permission', 'timeout'])
def test_failure_and_restart_reconcile(setup, monkeypatch, failure):
    factory, rid, calls, client = setup
    def handler(request):
        if 'auth/v3' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'mock', 'expire': 7200})
        if request.method == 'POST':
            if failure == 'timeout':
                raise httpx.ReadTimeout('secret remote content must not escape')
            return httpx.Response(403, json={'code': 99991672, 'msg': 'secret'})
        if request.method == 'GET':
            return httpx.Response(200, json={'code': 0, 'data': {'items': [
                {'reaction_id': 'own', 'operator': {'operator_type': 'app', 'operator_id': 'app'}, 'reaction_type': {'emoji_type': 'Typing'}},
                {'reaction_id': 'other', 'operator': {'operator_type': 'app', 'operator_id': 'other'}, 'reaction_type': {'emoji_type': 'Typing'}}], 'has_more': False}})
        calls.append(request.url.path)
        return httpx.Response(200, json={'code': 0})
    monkeypatch.setattr(reactions.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    reactions.process(rid, True, factory)
    with factory.begin() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'uncertain'
        assert 'secret' not in (row.error or '')
        db.get(Run, rid).status = 'failed'
        row.updated_at = now() - timedelta(seconds=31)
    reactions.recover(factory)
    assert calls == ['/open-apis/im/v1/messages/om_mock_new/reactions/own']
    with factory() as db:
        assert db.scalar(select(IMReaction)).state == 'cleared'


def test_cancel_pending_and_scope_change(setup, monkeypatch):
    factory, rid, calls, _ = setup
    monkeypatch.setenv('FEISHU_APP_ID', 'different-app')
    reactions.process(rid, True, factory)
    assert not calls
    with factory() as db:
        assert db.scalar(select(IMReaction)).error == 'APPLICATION_CHANGED'
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    with factory.begin() as db:
        db.get(Run, rid).status = 'cancelled'
    reactions.process(rid, factory=factory)
    assert not calls
    with factory() as db:
        assert db.scalar(select(IMReaction)).state == 'cleared'


def test_concurrent_create(setup):
    from concurrent.futures import ThreadPoolExecutor
    factory, rid, calls, _ = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: reactions.process(rid, True, factory), range(2)))
    assert len([x for x in calls if '/messages/' in x[1] and x[0] == 'POST']) == 1


def test_delete_failure_retries(setup, monkeypatch):
    factory, rid, calls, client = setup
    reactions.process(rid, True, factory)
    def failure(request):
        return httpx.Response(503, json={'code': 1, 'msg': 'private upstream body'})
    monkeypatch.setattr(reactions.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(failure), **kw))
    reactions.process(rid, factory=factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleanup_pending'
        assert row.message_id == 'om_mock_new'
        assert row.error == 'REACTION_API_FAILED'


def test_rejected_has_no_raw_id(database):
    with database.begin() as db:
        im._enqueue(db, 'feishu', 'raw-rejected', 'unknown', 'chat', 'hello', True)
    with database() as db:
        assert db.scalar(select(IMReaction)) is None
        assert 'raw-rejected' not in db.scalar(select(IMEvent)).event_id


@pytest.fixture
def dingtalk(database, monkeypatch):
    from app.models import Group, Identity, User
    from app.im_discovery import pin, scope
    monkeypatch.setenv('DINGTALK_CLIENT_SECRET', 'mock-only')
    with database.begin() as db:
        user = db.scalar(select(User))
        group = Group(name='钉钉群', org_id='org', team_id='team', member_ids=[user.id], provider='dingtalk', external_id='cidchat')
        identity = Identity(provider='dingtalk', external_user_id='staff1', user_id=user.id)
        db.add_all([group, identity])
        db.flush()
        pin(db, 'identity', identity.id, scope('dingtalk'))
        pin(db, 'group', group.id, scope('dingtalk'))
        im._enqueue(db, 'dingtalk', 'msg-ding-1', 'staff1', 'cidchat', 'hello', True, reply_mode='stream')
        run = db.scalar(select(Run))
        run.status = 'running'
        run_id = run.id
    calls, client = [], httpx.Client

    def install(monkeypatch, emotion):
        def handler(request):
            if request.url.path == '/v1.0/oauth2/accessToken':
                return httpx.Response(200, json={'accessToken': 'mock-token', 'expireIn': 7200})
            calls.append((request.url.path, request.headers.get('x-acs-dingtalk-access-token'), request.content))
            return emotion(request)
        monkeypatch.setattr(reactions.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    install(monkeypatch, lambda request: httpx.Response(200, json={'success': True}))
    im._token_cache.clear()
    return database, run_id, calls, install


def test_dingtalk_lifecycle_adds_and_recalls_the_emotion_on_the_users_message(dingtalk):
    import json
    factory, rid, calls, _ = dingtalk
    reactions.process(rid, True, factory)
    reactions.process(rid, True, factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'active' and row.message_id == 'msg-ding-1'
    reactions.process(rid, factory=factory)
    reactions.process(rid, factory=factory)
    assert [path for path, _, _ in calls] == ['/v1.0/robot/emotion/reply', '/v1.0/robot/emotion/recall']
    assert {token for _, token, _ in calls} == {'mock-token'}
    body = json.loads(calls[0][2])
    assert body['robotCode'] == 'robot' and body['openMsgId'] == 'msg-ding-1' and body['openConversationId'] == 'cidchat'
    assert body['emotionType'] == 2 and body['textEmotion']['text'] == reactions.DINGTALK_EMOTION
    assert json.loads(calls[1][2]) == body
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleared' and row.message_id is None and row.error is None


def test_dingtalk_webhook_messages_get_no_reaction(dingtalk):
    factory, _, _, _ = dingtalk
    with factory.begin() as db:
        im._enqueue(db, 'dingtalk', 'msg-ding-2', 'staff1', 'cidchat', 'hello again', True)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(IMReaction)) == 1


@pytest.mark.parametrize('answer', [httpx.Response(403, json={'code': 'Forbidden.AccessDenied', 'message': 'secret'}),
                                    httpx.Response(400, json={'code': 'invalidParameter', 'message': 'secret'}),
                                    httpx.Response(200, json={'success': False, 'message': 'secret'})])
def test_dingtalk_refusal_clears_without_retry_or_leak(dingtalk, monkeypatch, answer):
    factory, rid, calls, install = dingtalk
    install(monkeypatch, lambda request: answer)
    reactions.process(rid, True, factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleared' and row.message_id is None
        assert row.error == ('PERMISSION_REQUIRED' if answer.status_code == 403 else 'REACTION_API_FAILED')
        assert 'secret' not in row.error
    assert [path for path, _, _ in calls] == ['/v1.0/robot/emotion/reply']
    with factory() as db:
        assert reactions.status(db, 'dingtalk')['error'] == row.error


def test_dingtalk_timeout_is_recalled_after_the_run_ends(dingtalk, monkeypatch):
    factory, rid, calls, install = dingtalk
    def timeout(request):
        raise httpx.ReadTimeout('secret remote content must not escape')
    install(monkeypatch, timeout)
    reactions.process(rid, True, factory)
    with factory.begin() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'uncertain' and 'secret' not in (row.error or '')
        db.get(Run, rid).status = 'failed'
        row.updated_at = now() - timedelta(seconds=31)
    install(monkeypatch, lambda request: httpx.Response(200, json={'success': True}))
    reactions.recover(factory)
    assert [path for path, _, _ in calls][-1] == '/v1.0/robot/emotion/recall'
    with factory() as db:
        assert db.scalar(select(IMReaction)).state == 'cleared'


def test_dingtalk_recall_retries_transient_errors_but_gives_up_on_refusals(dingtalk, monkeypatch):
    factory, rid, calls, install = dingtalk
    reactions.process(rid, True, factory)
    install(monkeypatch, lambda request: httpx.Response(503, json={'message': 'private upstream body'}))
    reactions.process(rid, factory=factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleanup_pending' and row.message_id == 'msg-ding-1' and row.error == 'REACTION_API_FAILED'
    install(monkeypatch, lambda request: httpx.Response(404, json={'message': 'private upstream body'}))
    reactions.process(rid, factory=factory)
    with factory() as db:
        row = db.scalar(select(IMReaction))
        assert row.state == 'cleared' and row.message_id is None and row.error == 'REACTION_API_FAILED'


def test_dingtalk_other_application_is_left_alone(dingtalk, monkeypatch):
    factory, rid, calls, _ = dingtalk
    monkeypatch.setenv('DINGTALK_CLIENT_ID', 'different-app')
    reactions.process(rid, True, factory)
    assert not calls
    with factory() as db:
        assert db.scalar(select(IMReaction)).error == 'APPLICATION_CHANGED'
