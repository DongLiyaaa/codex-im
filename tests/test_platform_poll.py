"""Feishu device authorization against scripted HTTPS answers that mirror the real platform.

The real token endpoint answers "not authorized yet" with HTTP 400 {"error": "authorization_pending"}; earlier
tests returned HTTP 200 for it, so the very first real poll ended every authorization as platform_rejected.
"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from test_im_postgres import database
from test_platform_auth import configure, device
from test_platform_broker import request as start_authorization, opaque
from app import attachment_download, platform_auth as pa, platform_broker as broker, platform_worker as worker
from app.im_discovery import pin, scope
from app.models import Identity, PlatformAuthJob, PlatformConnection, User, now

ACCOUNTS, OPEN = 'accounts.feishu.cn', 'open.feishu.cn'
DEVICE_PATH, TOKEN_PATH, USER_PATH = '/oauth/v1/device_authorization', '/open-apis/authen/v2/oauth/token', '/open-apis/authen/v1/user_info'
DEVICE = {'device_code': 'device-code-value', 'user_code': 'ABCD-1234', 'expires_in': 600, 'interval': 5,
          'verification_uri_complete': 'https://accounts.feishu.cn/oauth/v1/device/verify?user_code=ABCD-1234'}
PENDING = (400, {'error': 'authorization_pending', 'error_description': 'The authorization request is still pending.', 'code': 20094})
SLOW_DOWN = (400, {'error': 'slow_down', 'code': 20095})


def token(scope='docx:document sheets:spreadsheet'):
    return (200, {'access_token': opaque('USER', 'TOKEN'), 'expires_in': 7200, 'token_type': 'Bearer', 'scope': scope})


class Script:
    """HTTPS answers per (host, path), consumed in order; the last answer repeats."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def add(self, host, path, *answers):
        self.routes[(host, path)] = list(answers)

    def install(self, monkeypatch):
        script = self

        class Response:
            def __init__(self, status, payload):
                self.status, self.raw = status, json.dumps(payload).encode()

            def read(self, limit=None):
                return self.raw

        class Connection:
            timeout = 0

            def __init__(self, host, address):
                self.host = host

            def request(self, method, url, body=None, headers=None):
                self.path = url.split('?')[0]
                script.calls.append((self.host, self.path))

            def getresponse(self):
                answers = script.routes[(self.host, self.path)]
                answer = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(answer, Exception):
                    raise answer
                return Response(*answer)

            def close(self):
                pass

        monkeypatch.setattr(attachment_download, 'PinnedHTTPS', Connection)
        monkeypatch.setattr(attachment_download, 'resolve_addresses', lambda host: ['203.0.113.7'])


@pytest.fixture
def flow(database, monkeypatch):
    configure(monkeypatch)
    script = Script()
    script.add(ACCOUNTS, DEVICE_PATH, (200, DEVICE))
    script.add(OPEN, USER_PATH, (200, {'code': 0, 'data': {'open_id': 'sender'}}))
    script.install(monkeypatch)
    sent = []
    monkeypatch.setattr(broker, 'dispatch', lambda *args: sent.append(args))
    user_id, _, _ = start_authorization(database, group=False)
    worker.tick(database)  # Begins the device authorization...
    worker.tick(database)  # ...and delivers the link privately.
    return database, script, sent, user_id


def poll_once(database, user_id, provider='feishu'):
    with database.begin() as db:
        db.get(PlatformConnection, (user_id, provider)).next_poll_at = now() - timedelta(seconds=1)
    worker.tick(database)
    with database() as db:
        row, job = db.get(PlatformConnection, (user_id, provider)), db.get(PlatformAuthJob, (user_id, provider))
        device = pa.unseal(row) if row.encrypted else {}
        return row.state, job.phase, job.error, device


def test_pending_http_400_keeps_waiting_until_the_user_approves(flow):
    database, script, sent, user_id = flow
    assert 'ABCD-1234' in sent[0][3]  # The link reached the user's private chat.
    script.add(OPEN, TOKEN_PATH, PENDING, SLOW_DOWN, PENDING, token('docx:document sheets:spreadsheet auth:user.id:read offline_access'))
    state, phase, error, device = poll_once(database, user_id)
    assert (state, phase, error) == ('pending', 'poll', None) and device['interval'] == 5  # The first poll used to kill the flow.
    state, _, _, device = poll_once(database, user_id)
    assert state == 'pending' and device['interval'] == 10  # slow_down widens the interval.
    assert poll_once(database, user_id)[0] == 'pending'
    state, phase, error, _ = poll_once(database, user_id)
    assert (state, phase, error) == ('connected', 'notify', None)
    worker.tick(database)  # Tells the user to resend the task.
    assert len(sent) == 2 and '重新发送' in sent[1][3] and opaque('USER', 'TOKEN') not in json.dumps(sent)
    with database() as db:
        stored = pa.unseal(db.get(PlatformConnection, (user_id, 'feishu')))
        assert {'docx:document', 'sheets:spreadsheet'} <= set(stored['scope'].split())


@pytest.mark.parametrize('answer,expected', [
    ((400, {'error': 'access_denied', 'code': 20095}), 'provider_denied'),
    ((400, {'error': 'expired_token', 'code': 20096}), 'expired'),
    ((400, {'error': 'invalid_grant'}), 'expired'),
    ((400, {'error': 'something_new', 'code': 20999}), 'platform_rejected'),
    ((200, {'access_token': ''}), 'platform_rejected'),
])
def test_terminal_answers_are_not_mistaken_for_pending(flow, answer, expected):
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, answer)
    state, phase, _, _ = poll_once(database, user_id)
    assert (state, phase) == (expected, 'done')


@pytest.mark.parametrize('scope,reason', [
    ('docx:document mail:user_mailbox:readonly', 'SCOPE_OUT_OF_DOMAIN:mail'),
    ('docx:document im:message calendar:calendar', 'SCOPE_OUT_OF_DOMAIN:calendar,im'),
    ('docx:document search:message', 'SCOPE_OUT_OF_DOMAIN:search'),
    ('sheets:spreadsheet', 'SCOPE_NOT_WRITABLE'),
    ('docx:document:readonly', 'SCOPE_NOT_WRITABLE'),
])
def test_token_without_document_write_or_with_other_domains_is_rejected_with_a_reason(flow, scope, reason):
    """The reason names only scope domains, so an administrator can see what to fix without any credential being kept."""
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, token(scope))
    state, phase, error, device = poll_once(database, user_id)
    assert (state, phase, error, device) == ('provider_invalid_config', 'done', reason, {})


def test_platform_baseline_scopes_do_not_fail_a_valid_authorization(flow):
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, token('docx:document auth:user.id:read offline_access'))
    assert poll_once(database, user_id)[0] == 'connected'


def test_wrong_person_authorizing_is_still_refused(flow):
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, token())
    script.add(OPEN, USER_PATH, (200, {'code': 0, 'data': {'open_id': 'someone-else'}}))
    state, _, _, device = poll_once(database, user_id)
    assert state == 'identity_mismatch' and device == {}  # The token is discarded, not stored.


def test_transient_failures_keep_waiting_and_recover(flow, monkeypatch):
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, (503, {}), ConnectionResetError('reset'), PENDING, token())
    state, phase, _, device = poll_once(database, user_id)
    assert (state, phase) == ('pending', 'poll') and device['failures'] == 1
    state, _, _, device = poll_once(database, user_id)
    assert state == 'pending' and device['failures'] == 2
    state, _, _, device = poll_once(database, user_id)
    assert state == 'pending' and device['failures'] == 0  # A healthy answer resets the budget.
    assert poll_once(database, user_id)[0] == 'connected'


def test_persistent_failures_end_the_authorization_with_a_clear_error(flow, monkeypatch):
    database, script, _, user_id = flow
    monkeypatch.setattr(worker, 'MAX_POLL_FAILURES', 3)
    script.add(OPEN, TOKEN_PATH, (503, {}))
    for _ in range(3):
        assert poll_once(database, user_id)[0] == 'pending'
    state, phase, error, _ = poll_once(database, user_id)
    assert (state, phase, error) == ('platform_rejected', 'done', 'PLATFORM_POLL_FAILED')


def test_failure_after_the_token_was_issued_is_terminal_and_never_polls_again(flow):
    """The device code is spent once a token is issued, so a later failure (here: the identity check) cannot be retried."""
    database, script, _, user_id = flow
    script.add(OPEN, TOKEN_PATH, token())
    script.add(OPEN, USER_PATH, ConnectionResetError('reset'))
    state, phase, error, device = poll_once(database, user_id)
    assert (state, phase, error, device) == ('platform_rejected', 'done', 'PLATFORM_POLL_FAILED', {})
    worker.tick(database)
    assert script.calls.count((OPEN, TOKEN_PATH)) == 1


def test_dingtalk_poll_failures_are_never_retried(database, monkeypatch):
    """DingTalk's one-time auth code must not be re-claimed, so its failures stay terminal."""
    configure(monkeypatch)
    monkeypatch.setattr(pa, 'begin', lambda provider: device(provider) | {'url': 'https://login.dingtalk.com/device'})
    monkeypatch.setenv('DINGTALK_TRANSPORT', 'stream')
    monkeypatch.setattr(broker, 'dispatch', lambda *args: None)
    with database.begin() as db:
        identity = Identity(user_id=db.scalar(select(User)).id, provider='dingtalk', external_user_id='staff-target')
        db.add(identity)
        db.flush()
        pin(db, 'identity', identity.id, scope('dingtalk'))
    user_id, _, _ = start_authorization(database, 'dingtalk', group=False)
    worker.tick(database)  # Begins the device authorization...
    worker.tick(database)  # ...and delivers the link privately.
    attempts = []

    def unreachable(*args):
        attempts.append(1)
        raise ConnectionResetError('reset')

    monkeypatch.setattr(pa, 'poll', unreachable)
    state, phase, error, _ = poll_once(database, user_id, 'dingtalk')
    assert (state, phase, error) == ('platform_rejected', 'done', 'PLATFORM_POLL_FAILED') and len(attempts) == 1


def test_request_returns_polling_answers_but_raises_every_other_http_error(flow):
    _, script, _, _ = flow
    url = 'https://' + OPEN + TOKEN_PATH
    script.add(OPEN, TOKEN_PATH, PENDING)
    assert pa.request('POST', url, data={})['error'] == 'authorization_pending'
    script.add(OPEN, TOKEN_PATH, (400, {'error': 'invalid_client'}))
    with pytest.raises(pa.ProviderError) as caught:
        pa.request('POST', url, data={})
    assert caught.value.state == 'provider_invalid_config'
    script.add(OPEN, TOKEN_PATH, (503, {}))
    with pytest.raises(ValueError):
        pa.request('POST', url, data={})


def test_a_pending_answer_to_the_device_request_itself_is_never_a_started_authorization(flow):
    _, script, _, _ = flow
    script.add(ACCOUNTS, DEVICE_PATH, PENDING)
    with pytest.raises(pa.ProviderError) as caught:
        pa.begin('feishu')
    assert caught.value.state == 'platform_rejected'
