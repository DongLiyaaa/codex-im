"""Isolated PostgreSQL + mock HTTP only; never touches historical platform messages."""
import httpx
import pytest
from sqlalchemy import select
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
