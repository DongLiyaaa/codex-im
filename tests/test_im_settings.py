import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_im_postgres import database
from app import im_settings as settings, im, main
from app.db import get_db
from app.models import IMSettings, IMConnection, now


@pytest.fixture
def client(database):
    def dependency():
        with database.begin() as db:
            yield db
    main.app.dependency_overrides[get_db] = dependency
    main.app.dependency_overrides[main.current_user] = lambda: SimpleNamespace(role='super_admin', id='root')
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def body(revision=0, **fields):
    return {'revision': revision, 'transport': 'websocket', 'fields': fields}


@pytest.mark.parametrize('role', ['org_admin', 'team_lead', 'member'])
def test_platform_permissions(client, role):
    main.app.dependency_overrides[main.current_user] = lambda: SimpleNamespace(role=role)
    assert client.get('/api/integrations/config/feishu').status_code == 403
    assert client.put('/api/integrations/config/feishu', json=body()).status_code == 403
    assert client.get('/api/integrations/status').status_code == 403


def test_encryption_retention_clear_and_fallback(client, database, monkeypatch):
    monkeypatch.setenv('FEISHU_APP_SECRET', 'environment-secret')
    r = client.put('/api/integrations/config/feishu', json=body(APP_ID='app', APP_SECRET='new-secret'))
    assert r.status_code == 200, r.text
    assert 'new-secret' not in r.text and 'environment-secret' not in r.text
    assert r.json()['secrets_set']['APP_SECRET']
    with database() as db:
        row = db.get(IMSettings, 'feishu')
        assert 'new-secret' not in row.encrypted
        with settings.snapshot(db, 'feishu'):
            assert im._required('FEISHU_APP_SECRET') == 'new-secret'
    assert client.put('/api/integrations/config/feishu', json=body(1, APP_SECRET='')).status_code == 200
    r = client.put('/api/integrations/config/feishu', json={**body(2), 'clear': ['APP_SECRET']})
    assert r.status_code == 200 and not r.json()['secrets_set']['APP_SECRET']
    with database() as db:
        with settings.snapshot(db, 'feishu'):
            assert 'FEISHU_APP_SECRET' in im.configuration('feishu')['missing']
    assert client.put('/api/integrations/config/feishu', json=body(1)).status_code == 409


@pytest.mark.parametrize('payload', [body(APP_SECRET='********'), body(UNKNOWN='x'), {**body(APP_SECRET='x'), 'clear': ['APP_SECRET']}, {**body(), 'transport':'bad'}])
def test_invalid_no_secret_echo(client, payload):
    r = client.put('/api/integrations/config/feishu', json=payload)
    assert r.status_code == 400
    assert '********' not in r.text


def test_key_rotation_fails_closed(database, monkeypatch):
    with database.begin() as db:
        settings.save(db, 'feishu', settings.Update(**body(APP_SECRET='secret')))
    monkeypatch.setenv('SESSION_SECRET', 'another-strong-secret-value-at-least-32-characters')
    with database() as db, pytest.raises(HTTPException) as exc:
        settings.read(db, 'feishu')
    assert exc.value.status_code == 503


def test_concurrent_initial_save(database):
    def save(_):
        try:
            with database.begin() as db:
                settings.save(db, 'feishu', settings.Update(**body(APP_ID='app')))
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, range(2))) == [200, 409]


def test_status_rejects_old_connection(client, database):
    client.put('/api/integrations/config/feishu', json=body(APP_ID='app', APP_SECRET='secret'))
    with database.begin() as db:
        values, _ = settings.effective(db, 'feishu')
        db.add(IMConnection(provider='feishu', transport='websocket', state='connected:' + settings.fingerprint(values), updated_at=now()))
    assert client.get('/api/integrations/status').json()['feishu']['state'] == 'connected'
    client.put('/api/integrations/config/feishu', json=body(1, APP_SECRET='rotated'))
    assert client.get('/api/integrations/status').json()['feishu']['state'] != 'connected'


def test_validation_does_not_echo_credentials(client):
    r = client.put('/api/integrations/config/feishu', json={**body(), 'fields': {'APP_SECRET': {'secret': 'do-not-echo'}}})
    assert r.status_code == 422 and 'do-not-echo' not in r.text


def test_old_sdk_ingress_rejected(database, monkeypatch):
    from app.im_connections import ensure_current
    monkeypatch.setenv('IM_CONFIG_FINGERPRINT', 'obsolete')
    with database() as db, pytest.raises(HTTPException) as exc:
        ensure_current(db, 'feishu')
    assert exc.value.status_code == 503


def test_supervisor_restart_and_clear(database, monkeypatch):
    from app import im_connections as connections
    with database.begin() as db:
        settings.save(db, 'feishu', settings.Update(**body(APP_ID='app', APP_SECRET='first')))
    monkeypatch.setattr(connections, 'SessionLocal', database)
    monkeypatch.setattr(connections, 'engine', database.kw['bind'])
    monkeypatch.setattr(connections.signal, 'signal', lambda *a: None)
    starts, stopped = [], []
    class Child:
        def poll(self): return None
    def spawn(args, env):
        starts.append(env['FEISHU_APP_SECRET'])
        return Child()
    monkeypatch.setattr(connections.subprocess, 'Popen', spawn)
    monkeypatch.setattr(connections, 'stop_child', lambda child: stopped.append(child) if child else None)
    class Event:
        n = 0
        def is_set(self): return self.n >= 3
        def wait(self, seconds):
            self.n += 1
            with database.begin() as db:
                if self.n == 1:
                    settings.save(db, 'feishu', settings.Update(**body(1, APP_SECRET='second')))
                elif self.n == 2:
                    settings.save(db, 'feishu', settings.Update(**{**body(2), 'clear': ['APP_SECRET']}))
    monkeypatch.setattr(connections.threading, 'Event', Event)
    connections.supervise('feishu')
    assert starts == ['first', 'second']
    assert len(stopped) == 2


def test_unknown_provider(client):
    assert client.get('/api/integrations/config/other').status_code == 404
