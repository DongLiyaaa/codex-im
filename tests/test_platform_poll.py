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
    # Reported as the identity check it was, not as the platform rejecting the authorization.
    assert (state, phase, error, device) == ('identity_unverified', 'done', 'IDENTITY_LOOKUP_FAILED', {})
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


@pytest.fixture
def dingtalk_flow(database, monkeypatch):
    """A DingTalk authorization delivered to the bound staff id, waiting for the user's approval."""
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
    worker.tick(database)
    worker.tick(database)

    def approve(**account):
        monkeypatch.setattr(pa, 'poll', lambda *args: ('connected', {'access_token': opaque('USER', 'TOKEN'), 'expires_in': 7200, **account}))
        return poll_once(database, user_id, 'dingtalk')

    def corp(value):
        with database.begin() as db:
            db.scalar(select(Identity).where(Identity.provider == 'dingtalk')).corp_id = value
    return approve, corp


def test_dingtalk_account_named_by_the_exchange_connects_without_directory_calls(dingtalk_flow, monkeypatch):
    """The CLI client's token is not an open-API token, and the robot has no member-read permission: neither is used."""
    approve, corp = dingtalk_flow
    corp('ding-corp')
    monkeypatch.setattr(pa, 'request', lambda *args, **kwargs: pytest.fail('identity must come from the token exchange'))
    state, phase, error, stored = approve(user_id='staff-target', corp_id='ding-corp')
    assert (state, phase, error) == ('connected', 'notify', None)
    assert (stored['user_id'], stored['corp_id']) == ('staff-target', 'ding-corp')


@pytest.mark.parametrize('account', [{'user_id': 'someone-else', 'corp_id': 'ding-corp'},
                                     # The same staff id in another organization is another person.
                                     {'user_id': 'staff-target', 'corp_id': 'other-corp'},
                                     {'user_id': 'staff-target'}])
def test_dingtalk_other_account_is_refused(dingtalk_flow, account):
    approve, corp = dingtalk_flow
    corp('ding-corp')
    assert approve(**account) == ('identity_mismatch', 'done', 'PLATFORM_IDENTITY_MISMATCH', {})


def test_dingtalk_unknown_robot_organization_is_unverified_not_rejected(dingtalk_flow):
    approve, _ = dingtalk_flow
    assert approve(user_id='staff-target', corp_id='ding-corp') == ('identity_unverified', 'done', 'IDENTITY_ORG_UNKNOWN', {})


MCP_GW, MCP_PATH = 'mcp-gw.dingtalk.com', '/server/db4b26cb38ea6a8739ad55d1997fa1da608cd36b33a6cf0f77884f70c49382fe'


def profile(payload, structured=True):
    """The contact MCP server's tools/call answer: the tool's JSON as text, and the same object structured."""
    result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}]}
    return (200, {'jsonrpc': '2.0', 'id': 1, 'result': result | ({'structuredContent': payload} if structured else {})})


def employee(corp, user):
    return {'orgEmployeeModel': {'corpId': corp, 'orgName': '组织', 'userId': user, 'orgUserName': '本人'}}


@pytest.fixture
def contact(dingtalk_flow, monkeypatch):
    """The CLI exchange named no account (as for this organization), so the contact MCP profile is asked."""
    approve, corp = dingtalk_flow
    corp('ding-corp')
    script = Script()
    script.install(monkeypatch)

    def answer(*answers):
        script.add(MCP_GW, MCP_PATH, *answers)
        return approve()
    return answer, script


@pytest.mark.parametrize('payload', [{'success': True, 'result': [employee('ding-corp', 'staff-target')]},
                                     # Several organizations: the robot's one decides.
                                     {'success': True, 'result': [employee('other-corp', 'staff-x'), employee('ding-corp', 'staff-target')]},
                                     {'result': {'corpId': 'ding-corp', 'userid': 'staff-target'}},
                                     # An account without organization is accepted only as the single one.
                                     {'result': [{'orgEmployeeModel': {'orgUserId': 'staff-target'}}]}])
def test_dingtalk_contact_profile_naming_the_bound_person_connects(contact, payload):
    answer, script = contact
    state, phase, error, stored = answer(profile(payload))
    assert (state, phase, error) == ('connected', 'notify', None) and 'user_id' not in stored
    assert script.calls == [(MCP_GW, MCP_PATH)]  # No open-API or robot directory call.


def test_dingtalk_contact_profile_text_only_answer_is_read(contact):
    answer, _ = contact
    assert answer(profile({'result': [employee('ding-corp', 'staff-target')]}, structured=False))[:3] == ('connected', 'notify', None)


@pytest.mark.parametrize('payload', [{'result': [employee('ding-corp', 'someone-else')]},
                                     {'result': [employee('other-corp', 'staff-target')]},
                                     {'result': [{'orgEmployeeModel': {'userId': 'staff-target'}}, {'orgEmployeeModel': {'userId': 'staff-x'}}]},
                                     {'result': []}])
def test_dingtalk_contact_profile_naming_someone_else_is_refused(contact, payload):
    answer, _ = contact
    assert answer(profile(payload)) == ('identity_mismatch', 'done', 'PLATFORM_IDENTITY_MISMATCH', {})


@pytest.mark.parametrize('answer_, reason', [
    (profile({'success': False, 'code': 'PAT_LOW_RISK_NO_PERMISSION', 'message': '无权限'}), 'IDENTITY_CONTACT_DENIED'),
    (profile({'success': False, 'code': 'TOKEN_VERIFIED_FAILED', 'error': 'Token验证失败'}), 'IDENTITY_TOKEN_REJECTED'),
    (profile({'success': False, 'code': 'SOMETHING_NEW'}), 'IDENTITY_LOOKUP_FAILED'),
    ((401, {'error': 'unauthorized'}), 'IDENTITY_LOOKUP_FAILED'),
    ((503, {}), 'IDENTITY_LOOKUP_FAILED'),
    (ConnectionResetError('reset'), 'IDENTITY_LOOKUP_FAILED'),
    ((200, {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32601}}), 'IDENTITY_LOOKUP_FAILED')])
def test_dingtalk_contact_profile_failures_are_unverified_with_their_reason(contact, database, answer_, reason):
    answer, _ = contact
    assert answer(answer_) == ('identity_unverified', 'done', reason, {})
    with database.begin() as db:
        shown = broker.status(db, db.scalar(select(User)), 'dingtalk')
    assert (shown['state'], shown['error_code'], shown['message']) == ('identity_unverified', reason, pa.UNVERIFIED[reason])


def test_dingtalk_unknown_organization_asks_nobody(dingtalk_flow, monkeypatch):
    approve, _ = dingtalk_flow
    monkeypatch.setattr(pa, 'request', lambda *args, **kwargs: pytest.fail('the staff id cannot be matched without the organization'))
    assert approve() == ('identity_unverified', 'done', 'IDENTITY_ORG_UNKNOWN', {})


def test_platform_refusing_the_identity_call_is_unverified_not_rejected(monkeypatch):
    def refuse(*args, **kwargs):
        raise pa.ProviderError('platform_rejected')
    monkeypatch.setattr(pa, 'request', refuse)
    with pytest.raises(pa.ProviderError) as caught:
        worker.verified(None, 'feishu', {'access_token': 'T'}, 'sender', {})
    assert (caught.value.state, caught.value.reason) == ('identity_unverified', 'IDENTITY_LOOKUP_FAILED')


def test_internal_dingtalk_message_records_the_robot_organization(database, monkeypatch):
    from app.im import _enqueue
    from app.models import uid
    monkeypatch.setenv('DINGTALK_TRANSPORT', 'stream')
    with database.begin() as db:
        identity = Identity(user_id=db.scalar(select(User)).id, provider='dingtalk', external_user_id='staff-target')
        db.add(identity)
        db.flush()
        pin(db, 'identity', identity.id, scope('dingtalk'))

    def message(**origin):
        with database.begin() as db:
            _enqueue(db, 'dingtalk', uid(), 'staff-target', 'private-chat', '你好', False, reply_mode='stream', **origin)
        with database() as db:
            return db.scalar(select(Identity.corp_id).where(Identity.provider == 'dingtalk'))
    assert message(sender_internal=False, corp_id='ding-corp') is None  # An external sender never names the organization.
    assert message(sender_internal=True, corp_id='ding-corp') == 'ding-corp'
