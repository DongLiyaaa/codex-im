"""The home-page Codex check: super administrator only, a fixed set of facts, and no upstream text on any failure."""
import json
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_im_postgres import database
from test_setup import http
from app import codex_status, main, security
from app.models import User

CANARY = 'leak-canary-5d0b7e21'
TOKEN = 't' * 40
STATUS = {'ready': True, 'checked_at': 1790000000, 'auth_mode': 'api',
          'codex': {'installed': True, 'version': '0.157.1', 'pinned_version': '0.157.1'},
          'model': {'id': 'demo-model', 'endpoint_host': 'models.example.com', 'credential_configured': True},
          'config_error': None, 'sandbox': {'state': 'ok', 'error': None},
          'model_endpoint': {'state': 'ok', 'http_status': 200, 'model_listed': True}}


def reply(status=200, body=None, headers=None):
    payload = body if isinstance(body, bytes) else json.dumps(STATUS if body is None else body).encode()
    return status, payload, headers or {}


@contextmanager
def server(respond, delay=0.0):
    """A stand-in runner on a free local port; records what it was asked."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
            if delay:
                time.sleep(delay)
            status, payload, headers = respond(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}', seen
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def runner_settings(monkeypatch):
    monkeypatch.setenv('RUNNER_TOKEN', TOKEN)
    monkeypatch.setenv('RUNNER_URL', 'http://127.0.0.1:1')


def read(url, monkeypatch, refresh=False):
    monkeypatch.setenv('RUNNER_URL', url)
    return codex_status.check(refresh)


def test_the_check_returns_only_the_known_facts(monkeypatch):
    noisy = dict(STATUS, api_key=CANARY, model=dict(STATUS['model'], secret=CANARY), extra={'nested': CANARY})
    with server(lambda _: reply(body=noisy)) as (url, seen):
        result = read(url, monkeypatch)
    assert result == {'reachable': True, 'error': None, 'runner': STATUS | {'model_endpoint': STATUS['model_endpoint'] | {'reason': None}}}
    assert CANARY not in json.dumps(result) and TOKEN not in json.dumps(result)
    assert seen == [{'path': '/status', 'authorization': f'Bearer {TOKEN}'}]


def test_refresh_is_forwarded_only_when_asked(monkeypatch):
    with server(lambda _: reply()) as (url, seen):
        read(url, monkeypatch)
        read(url, monkeypatch, refresh=True)
    assert [entry['path'] for entry in seen] == ['/status', '/status?refresh=true']


@pytest.mark.parametrize('code,expected', [(401, 'RUNNER_AUTH_FAILED'), (404, 'RUNNER_OUTDATED'),
                                           (503, 'RUNNER_TOKEN_NOT_CONFIGURED'), (504, 'RUNNER_TIMEOUT'),
                                           (500, 'RUNNER_ERROR'), (429, 'RUNNER_ERROR')])
def test_runner_errors_become_static_codes(monkeypatch, code, expected):
    with server(lambda _: reply(code, {'detail': f'{CANARY} http://internal.example/x'})) as (url, _):
        result = read(url, monkeypatch)
    assert result == {'reachable': False, 'error': expected, 'runner': None}


@pytest.mark.parametrize('body', [b'not json', b'[]', b'{}', b'null',
                                  json.dumps(STATUS | {'auth_mode': 'other'}).encode(),
                                  json.dumps(STATUS | {'ready': 'yes please'}).encode(),
                                  json.dumps(STATUS | {'model_endpoint': {'state': 'exploded'}}).encode(),
                                  json.dumps(STATUS | {'config_error': 'x' * 500}).encode(),
                                  b'{"ready": true,' + b' ' * (codex_status.LIMIT + 1) + b'}'])
def test_malformed_or_oversized_answers_are_rejected(monkeypatch, body):
    with server(lambda _: reply(body=body)) as (url, _):
        result = read(url, monkeypatch)
    assert result == {'reachable': False, 'error': 'RUNNER_INVALID_RESPONSE', 'runner': None}


def test_a_runner_that_is_down(monkeypatch):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    assert read(f'http://127.0.0.1:{port}', monkeypatch)['error'] == 'RUNNER_UNREACHABLE'
    assert read('http://[bad', monkeypatch)['error'] == 'RUNNER_UNREACHABLE'


def test_a_runner_that_does_not_answer_in_time(monkeypatch):
    monkeypatch.setattr(codex_status, 'TIMEOUT', httpx.Timeout(0.3, connect=0.3))
    with server(lambda _: reply(), delay=1.0) as (url, _):
        assert read(url, monkeypatch)['error'] == 'RUNNER_TIMEOUT'


def test_without_a_token_nothing_is_sent(monkeypatch):
    monkeypatch.delenv('RUNNER_TOKEN')
    with server(lambda _: reply()) as (url, seen):
        assert read(url, monkeypatch) == {'reachable': False, 'error': 'RUNNER_TOKEN_NOT_CONFIGURED', 'runner': None}
    assert seen == []


def test_a_redirect_is_never_followed_with_the_token(monkeypatch):
    with server(lambda _: reply()) as (elsewhere, hits):
        with server(lambda _: reply(302, b'', {'Location': elsewhere + '/steal'})) as (url, _):
            result = read(url, monkeypatch)
    assert result == {'reachable': False, 'error': 'RUNNER_ERROR', 'runner': None}
    assert hits == []


def login(client, email, secret):
    assert client.post('/api/auth/login', json={'email': email, 'password': secret}).status_code == 200


def test_only_the_super_administrator_can_read_it(database, monkeypatch):
    secrets_by_role = {role: secrets.token_urlsafe(18) for role in ('super_admin', 'org_admin', 'member')}
    with database.begin() as db:
        for role, secret in secrets_by_role.items():
            db.add(User(email=f'{role}@example.invalid', name=role, role=role, active=True, org_id='org', team_id='team',
                        password_hash=security.hash_password(secret)))
    boss, anonymous = http(database), TestClient(main.app)
    others = {role: TestClient(main.app) for role in ('org_admin', 'member')}
    with server(lambda _: reply()) as (url, seen):
        monkeypatch.setenv('RUNNER_URL', url)
        assert anonymous.get('/api/system/codex-status').status_code == 401
        login(boss, 'super_admin@example.invalid', secrets_by_role['super_admin'])
        for role, client in others.items():
            login(client, f'{role}@example.invalid', secrets_by_role[role])
            assert client.get('/api/system/codex-status').status_code == 403, role
        assert seen == []  # Refused callers never reach the runner.
        response = boss.get('/api/system/codex-status')
        assert response.status_code == 200 and response.json()['reachable'] is True
        assert response.json()['runner']['model']['id'] == 'demo-model'
        assert boss.get('/api/system/codex-status?refresh=true').status_code == 200
        assert [entry['path'] for entry in seen] == ['/status', '/status?refresh=true']
        assert TOKEN not in response.text


def test_a_deactivated_super_administrator_is_refused(database, monkeypatch):
    secret = secrets.token_urlsafe(18)
    with database.begin() as db:
        db.add(User(email='root@example.invalid', name='root', role='super_admin', active=True,
                    password_hash=security.hash_password(secret)))
    boss = http(database)
    with server(lambda _: reply()) as (url, seen):
        monkeypatch.setenv('RUNNER_URL', url)
        login(boss, 'root@example.invalid', secret)
        with database.begin() as db:
            db.scalar(select(User).where(User.email == 'root@example.invalid')).active = False
        assert boss.get('/api/system/codex-status').status_code in (401, 403)
        assert seen == []


def test_claude_agent_facts_are_passed_through_and_filtered(monkeypatch):
    claude = {'enabled': True, 'ready': True, 'installed': True, 'version': '2.1.286', 'pinned_version': '2.1.286',
              'model': {'id': 'claude-x', 'endpoint_host': 'gateway.example.com', 'credential_configured': True,
                        'api_key': CANARY},
              'config_error': None, 'model_endpoint': {'state': 'ok', 'http_status': 200, 'model_listed': True},
              'secret': CANARY}
    body = dict(STATUS, agents={'codex': {'enabled': True, 'ready': True}, 'claude': claude})
    with server(lambda _: reply(body=body)) as (url, _):
        result = read(url, monkeypatch)
    agents = result['runner']['agents']
    assert agents['codex'] == {'enabled': True, 'ready': True}
    assert agents['claude']['ready'] and agents['claude']['model']['endpoint_host'] == 'gateway.example.com'
    assert CANARY not in json.dumps(result)


def test_a_disabled_claude_agent_reports_only_its_state(monkeypatch):
    body = dict(STATUS, agents={'codex': {'enabled': True, 'ready': True}, 'claude': {'enabled': False, 'ready': False}})
    with server(lambda _: reply(body=body)) as (url, _):
        result = read(url, monkeypatch)
    assert result['runner']['agents']['claude']['enabled'] is False and result['runner']['agents']['claude']['model'] is None
