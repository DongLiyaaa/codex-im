import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_im_postgres import database
from test_setup import http
from app import codex_auth as auth, main, security, service, user_lifecycle
from app.models import CodexModelQuery, CodexConnection, CodexLoginJob, Conversation, Message, Run, User, now

RAW = json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'test-access', 'refresh_token': 'test-refresh', 'id_token': 'test-id'}})


def seed(db, email):
    user = User(email=email, name='Test', role='super_admin', active=True, password_hash=security.hash_password('test-password-123'))
    db.add(user); db.flush()
    return user


def test_durable_job_commits_credentials_before_cleanup_and_cancellation(database, monkeypatch):
    monkeypatch.setattr(auth, 'mode', lambda: {'mode': 'personal'})
    calls = []
    def runner(method, path, body=None):
        calls.append((method, path))
        if method == 'POST': return {'state': 'pending', 'url': auth.VERIFY_URL, 'user_code': 'ABCD-1234'}
        if method == 'DELETE': return {'ok': True}
        return {'state': 'connected', 'auth': RAW, 'account': {'email': 'owner@example.invalid'}}
    monkeypatch.setattr(auth, 'runner', runner)
    with database.begin() as db:
        user = seed(db, 'first@example.invalid')
        auth.start(db, user)
        identifier = db.get(CodexConnection, user.id).generation
    auth.perform(identifier, database)
    with database() as db:
        assert auth.public(db, user)['user_code'] == 'ABCD-1234'
    auth.perform(identifier, database)
    with database() as db:
        row = db.get(CodexConnection, user.id)
        assert row.state == 'connected' and RAW not in row.encrypted
        assert auth.unseal(row.encrypted)['auth'] == RAW
        assert db.get(CodexLoginJob, identifier).phase == 'cleanup'
        assert db.scalar(select(CodexModelQuery.id).where(CodexModelQuery.user_id == user.id))
    assert not any(method == 'DELETE' for method, _ in calls)
    auth.perform(identifier, database)
    with database.begin() as db:
        assert db.get(CodexConnection, user.id).state == 'connected'
        assert db.get(CodexLoginJob, identifier).phase == 'done'
        auth.invalidate(db, user.id)
    with database() as db:
        assert db.get(CodexConnection, user.id).encrypted == ''


def test_duplicate_login_is_serialized_per_user(database):
    with database.begin() as db:
        user = seed(db, 'first@example.invalid')
    barrier = Barrier(2)
    def begin():
        barrier.wait()
        with database.begin() as db:
            auth.start(db, db.get(User, user.id))
            return db.get(CodexConnection, user.id).generation
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: begin(), range(2)))
    assert ids[0] == ids[1]
    with database() as db:
        assert len(list(db.scalars(select(CodexLoginJob)))) == 1


def test_http_users_cannot_claim_or_disconnect_each_others_credentials(database, monkeypatch):
    monkeypatch.setenv('RUNNER_TOKEN', 't' * 40)
    monkeypatch.setattr(auth, 'mode', lambda: {'mode': 'personal'})
    with database.begin() as db:
        first, second = seed(db, 'one@example.invalid'), seed(db, 'two@example.invalid')
        for user in (first, second):
            row = auth.locked(db, user.id)
            row.state, row.encrypted = 'connected', auth.seal({'auth': RAW})
    client = http(database)
    try:
        assert client.post('/api/auth/login', json={'email': first.email, 'password': 'test-password-123'}).status_code == 200
        assert client.post('/api/auth/codex/disconnect', json={'user_id': second.id}).status_code == 200
        with database() as db:
            assert db.get(CodexConnection, first.id).state == 'disconnected'
            assert db.get(CodexConnection, second.id).state == 'connected'
        assert RAW not in client.get('/api/auth/codex').text
        assert client.post('/internal/codex-auth/unknown/claim', json={'ticket': 'guess'}).status_code == 401
    finally:
        client.close(); main.app.dependency_overrides.clear()


def test_revoked_generation_rejects_late_refresh(database):
    with database.begin() as db:
        user = seed(db, 'one@example.invalid')
        row = auth.locked(db, user.id)
        row.state, row.encrypted = 'connected', auth.seal({'auth': RAW})
        conversation = Conversation(owner_id=user.id, title='Test')
        db.add(conversation); db.flush()
        message = Message(conversation_id=conversation.id, role='user', content='Test')
        db.add(message); db.flush()
        run = Run(conversation_id=conversation.id, user_id=user.id, message_id=message.id, status='running')
        db.add(run); db.flush()
        ticket = auth.reserve(db, run)
    client = http(database)
    headers = {'Authorization': 'Bearer ' + 't' * 40}
    try:
        response = client.post(f'/internal/codex-auth/{run.id}/claim', json={'ticket': ticket}, headers=headers)
        assert response.status_code == 200
        with database.begin() as db: auth.invalidate(db, user.id)
        response = client.post(f'/internal/codex-auth/{run.id}/save', json={'ticket': ticket, 'auth': RAW}, headers=headers)
        assert response.status_code == 403
        with database() as db: assert db.get(CodexConnection, user.id).encrypted == ''
    finally:
        client.close(); main.app.dependency_overrides.clear()


def test_model_catalog_task_serializes_refresh_and_retains_selection(database, monkeypatch):
    monkeypatch.setenv('RUNNER_TOKEN', 't' * 40)
    with database.begin() as db:
        user = seed(db, 'model@example.invalid')
        row = auth.locked(db, user.id)
        row.state, row.encrypted = 'connected', auth.seal({'auth': RAW})
        auth.queue_models(db, user)
        identifier = row.lease_run_id
    def runner(method, path, body=None):
        if method == 'POST': return {'state': 'starting'}
        if method == 'DELETE': return {'ok': True}
        return {'state': 'connected', 'auth': RAW, 'models': [{'model': 'actual-model', 'display_name': 'Actual model', 'is_default': True}]}
    monkeypatch.setattr(auth, 'runner', runner)
    auth.perform_models(identifier, database)
    auth.perform_models(identifier, database)
    with database() as db:
        row = db.get(CodexConnection, user.id)
        assert row.saved and row.lease_run_id == identifier
        assert auth.unseal(row.encrypted)['models'][0]['model'] == 'actual-model'
    auth.perform_models(identifier, database)
    with database() as db:
        row = db.get(CodexConnection, user.id)
        assert row.lease_run_id is None and row.state == 'connected'
        assert db.get(CodexModelQuery, identifier).phase == 'done'
