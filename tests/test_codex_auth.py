import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import codex_auth as auth, main
from app.models import CodexConnection, CodexLoginJob, Run, User, now

RAW = json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'private-access', 'refresh_token': 'private-refresh', 'id_token': 'private-id'}})


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', 'test-only-encryption-key-' + 's' * 40)
    monkeypatch.setenv('RUNNER_TOKEN', 't' * 40)


class Store:
    def __init__(self, *rows):
        self.rows = {(type(row), getattr(row, 'id', None) or getattr(row, 'user_id', None)): row for row in rows}
    def get(self, cls, key, **kwargs):
        return self.rows.get((cls, key))


def connection(user_id='u'):
    return CodexConnection(user_id=user_id, generation='g', state='connected', encrypted=auth.seal({'auth': RAW}),
                           account={}, claimed=False, saved=False)


def test_cipher_does_not_store_plaintext_or_accept_wrong_key(monkeypatch):
    from cryptography.fernet import InvalidToken
    encoded = auth.seal({'auth': RAW})
    assert 'private-access' not in encoded
    assert auth.unseal(encoded)['auth'] == RAW
    monkeypatch.setenv('SESSION_SECRET', 'other-secret-' + 'x' * 40)
    with pytest.raises(InvalidToken): auth.unseal(encoded)


@pytest.mark.parametrize('raw', ['null', '[]', '{}', 'x', json.dumps({'auth_mode': 'apikey'})])
def test_invalid_credential_formats(raw):
    with pytest.raises(ValueError): auth.valid_auth(raw)


def test_public_status_never_exposes_credentials_and_only_owner_gets_device_code():
    first, second = connection('u1'), connection('u2')
    first.state, first.generation = 'pending', 'job'
    job = CodexLoginJob(id='job', user_id='u1', delivery='web', expires_at=now() + timedelta(minutes=5),
                        encrypted_device=auth.seal({'url': auth.VERIFY_URL, 'user_code': 'ABCD-1234'}))
    db = Store(first, second, job)
    shown = auth.public(db, SimpleNamespace(id='u1'))
    assert shown['user_code'] == 'ABCD-1234'
    assert 'private-access' not in json.dumps(shown)
    assert 'user_code' not in auth.public(db, SimpleNamespace(id='u2'))
    job.delivery = 'delivered'
    assert 'user_code' not in auth.public(db, SimpleNamespace(id='u1'))


def test_lease_exclusivity_and_uncertain_refresh_fails_closed(monkeypatch):
    row = connection()
    monkeypatch.setattr(auth, 'locked', lambda db, user_id: row)
    run = Run(id='r', user_id='u', agent='codex', status='running')
    db = Store(User(id='u', active=True))
    ticket = auth.reserve(db, run)
    assert len(ticket) == 43 and ticket not in row.ticket_hash
    with pytest.raises(RuntimeError): auth.reserve(db, Run(id='r2', user_id='u', status='running'))
    row.claimed = True
    auth.finish(None, run, ticket)
    assert row.state == 'expired' and row.encrypted == ''


def request(body):
    sent = False
    async def receive():
        nonlocal sent
        if sent: return {'type': 'http.disconnect'}
        sent = True
        return {'type': 'http.request', 'body': json.dumps(body).encode(), 'more_body': False}
    return Request({'type': 'http', 'method': 'POST', 'headers': [(b'authorization', ('Bearer ' + 't' * 40).encode())]}, receive)


def test_claim_save_and_disconnect_generation_prevents_late_writes(monkeypatch):
    from app import service
    row, other = connection('u'), connection('other')
    user = User(id='u', active=True)
    run = Run(id='r', user_id='u', agent='codex', status='running')
    second = Run(id='r2', user_id='other', agent='codex', status='running')
    db = Store(row, other, run, second, user)
    monkeypatch.setattr(auth, 'locked', lambda db, user_id: row if user_id == 'u' else other)
    monkeypatch.setattr(service, 'build_payload', lambda db, run: {})
    ticket = auth.reserve(db, run)
    async def call(operation, body, identifier='r'):
        return await auth.credential_exchange(identifier, operation, request(body), db)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call('claim', {'ticket': ticket}, 'r2'))
    assert exc.value.status_code == 403
    assert asyncio.run(call('claim', {'ticket': ticket})) == {'auth': RAW}
    with pytest.raises(HTTPException): asyncio.run(call('claim', {'ticket': ticket}))
    rotated = RAW.replace('private-refresh', 'rotated-refresh')
    assert asyncio.run(call('save', {'ticket': ticket, 'auth': rotated})) == {'ok': True}
    assert asyncio.run(call('save', {'ticket': ticket, 'auth': rotated})) == {'ok': True}
    assert row.saved and auth.unseal(row.encrypted)['auth'] == rotated
    row.ticket_hash = None
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call('save', {'ticket': ticket, 'auth': RAW}))
    assert exc.value.status_code == 403


def test_self_service_routes_use_current_user_not_supplied_user_id(monkeypatch):
    actor = SimpleNamespace(id='me')
    seen = []
    main.app.dependency_overrides[auth.current_user] = lambda: actor
    main.app.dependency_overrides[auth.get_db] = lambda: object()
    monkeypatch.setattr(auth, 'mode', lambda: {'mode': 'personal', 'error': None})
    monkeypatch.setattr(auth, 'start', lambda db, user: seen.append(user.id) or {'state': 'starting'})
    client = TestClient(main.app)
    try:
        response = client.post('/api/auth/codex/start', json={'user_id': 'victim'})
        assert response.status_code == 200 and seen == ['me']
        response = client.post('/internal/codex-auth/r/claim', json={'ticket': 'anything'})
        assert response.status_code == 401
    finally:
        client.close()
        main.app.dependency_overrides.clear()


def test_im_group_authorization_never_starts_or_displays_a_code(monkeypatch):
    from app import im_commands
    queued = []
    db = SimpleNamespace(add=queued.append, flush=lambda: None)
    monkeypatch.setattr(auth, 'start', lambda *a, **kw: pytest.fail('Group started login'))
    im_commands.handle(db, 'codex-login', 'feishu', SimpleNamespace(id='event'), SimpleNamespace(id='u'),
                       SimpleNamespace(id='group'), None)
    assert '请私聊' in queued[0].text
    assert 'http' not in queued[0].text
