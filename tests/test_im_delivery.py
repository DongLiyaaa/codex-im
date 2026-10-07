"""Outbound delivery: reply anchoring, HTTP 400 fallbacks, idempotent retries, card normalization."""
import json

import httpx
import pytest
from sqlalchemy import select

from test_im_postgres import database
from test_im_connections import feishu, ding, ding_db
from app import im, im_connections as connections
from app.models import IMEvent, Run


@pytest.fixture
def wire(monkeypatch):
    """Returns deliver(handler, database, text): runs one reply through a mocked platform."""
    monkeypatch.setattr(im.time, 'sleep', lambda seconds: None)
    for name, value in (('FEISHU_APP_ID', 'app'), ('FEISHU_APP_SECRET', 'secret'), ('DINGTALK_CLIENT_ID', 'app'),
                        ('DINGTALK_CLIENT_SECRET', 'secret')):
        monkeypatch.setenv(name, value)
    im._token_cache.clear()
    real_client = httpx.Client

    def deliver(handler, database, text='reply'):
        monkeypatch.setattr(im.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
        with database.begin() as db:
            event = db.scalar(select(IMEvent))
            im.deliver_reply(db, db.scalar(select(Run)), text)
            return event.delivered_at is not None, event.delivery_error
    return deliver


def token_or(handler):
    def wrapped(request):
        if 'tenant_access_token' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
        return handler(request)
    return wrapped


def test_group_answer_replies_to_the_question(database, wire):
    connections.receive_feishu(feishu(kind='group', message_id='om_question'), database)
    seen = []
    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={'code': 0})
    assert wire(token_or(handler), database) == (True, None)
    [(path, body)] = seen
    assert path == '/open-apis/im/v1/messages/om_question/reply'
    assert body['msg_type'] == 'interactive' and 'receive_id' not in body and body['uuid'].endswith('-0')


def test_private_answer_is_a_plain_chat_message(database, wire):
    connections.receive_feishu(feishu(kind='p2p', message_id='om_question'), database)
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={'code': 0})
    assert wire(token_or(handler), database)[0] and paths == ['/open-apis/im/v1/messages']


@pytest.mark.parametrize('code', [230011, 99992354])
def test_recalled_question_falls_back_to_a_chat_message_with_the_same_uuid(database, wire, code):
    connections.receive_feishu(feishu(kind='group', message_id='om_question'), database)
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body['uuid']))
        if request.url.path.endswith('/reply'):
            return httpx.Response(400, json={'code': code, 'msg': 'The message was withdrawn.'})  # Feishu uses HTTP 400
        return httpx.Response(200, json={'code': 0})
    assert wire(token_or(handler), database) == (True, None)
    assert [p for p, _ in seen] == ['/open-apis/im/v1/messages/om_question/reply', '/open-apis/im/v1/messages']
    assert len({u for _, u in seen}) == 1


def test_http_400_card_rejection_falls_back_to_plain_text(database, wire):
    """Feishu rejects cards with HTTP 400 + a JSON code; the old fallback only handled HTTP 200 + code."""
    connections.receive_feishu(feishu(kind='p2p'), database)
    kinds = []
    def handler(request):
        body = json.loads(request.content)
        kinds.append(body['msg_type'])
        if body['msg_type'] == 'interactive':
            return httpx.Response(400, json={'code': 230099, 'msg': 'card table number over limit'})
        return httpx.Response(200, json={'code': 0})
    assert wire(token_or(handler), database) == (True, None)
    assert kinds == ['interactive', 'text']


def test_permission_errors_try_text_once_then_fail_cleanly_without_retrying(database, wire):
    connections.receive_feishu(feishu(kind='p2p'), database)
    kinds = []
    def handler(request):
        kinds.append(json.loads(request.content)['msg_type'])
        return httpx.Response(403, json={'code': 99991672, 'msg': 'no permission'})
    assert wire(token_or(handler), database) == (False, 'IM_DELIVERY_FAILED')
    assert kinds == ['interactive', 'text']  # A rejected card was never sent; no retry for a permanent error.


@pytest.mark.parametrize('failure', ['connect', 'timeout', 'http_503', 'http_429', 'rate_limit_code'])
def test_transient_failures_retry_with_the_same_uuid(database, wire, failure):
    connections.receive_feishu(feishu(kind='p2p'), database)
    uuids = []
    def handler(request):
        uuids.append(json.loads(request.content)['uuid'])
        if len(uuids) == 1:
            if failure == 'connect':
                raise httpx.ConnectError('down')
            if failure == 'timeout':
                raise httpx.ReadTimeout('slow')  # Ambiguous, but Feishu dedupes by uuid.
            if failure == 'http_503':
                return httpx.Response(503, json={})
            if failure == 'http_429':
                return httpx.Response(429, json={})
            return httpx.Response(400, json={'code': 99991400, 'msg': 'frequency limit'})
        return httpx.Response(200, json={'code': 0})
    assert wire(token_or(handler), database) == (True, None)
    assert len(uuids) == 2 and uuids[0] == uuids[1]


def test_retry_budget_is_bounded(database, wire):
    connections.receive_feishu(feishu(kind='p2p'), database)
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={})
    assert wire(token_or(handler), database) == (False, 'IM_DELIVERY_FAILED')
    assert len(calls) == len(im.RETRY_DELAYS) + 1


def test_rejected_access_token_is_refreshed_once(database, wire):
    connections.receive_feishu(feishu(kind='p2p'), database)
    tokens, sends = [], []
    def handler(request):
        if 'tenant_access_token' in request.url.path:
            tokens.append(1)
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token-%d' % len(tokens), 'expire': 7200})
        sends.append(request.headers['authorization'])
        if len(sends) == 1:
            return httpx.Response(400, json={'code': 99991663, 'msg': 'Invalid access token'})
        return httpx.Response(200, json={'code': 0})
    assert wire(handler, database) == (True, None)
    assert sends == ['Bearer token-1', 'Bearer token-2'] and len(tokens) == 2


def test_token_refresh_does_not_loop(database, wire):
    connections.receive_feishu(feishu(kind='p2p'), database)
    sends = []
    def handler(request):
        if 'tenant_access_token' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
        sends.append(1)
        return httpx.Response(400, json={'code': 99991663, 'msg': 'Invalid access token'})
    assert wire(handler, database) == (False, 'IM_DELIVERY_FAILED')
    assert len(sends) == 4  # Card + one refresh retry, then text + one refresh retry; never an endless loop.


@pytest.mark.parametrize('failure,expected_calls', [('connect', 2), ('timeout', 1), ('http_429', 2), ('http_500', 1)])
def test_dingtalk_retries_only_when_the_request_never_arrived(ding_db, wire, failure, expected_calls):
    connections.receive_dingtalk(ding(kind='1'), ding_db)
    calls = []
    def handler(request):
        if request.url.path.endswith('/accessToken'):
            return httpx.Response(200, json={'accessToken': 'token', 'expireIn': 7200})
        calls.append(1)
        if len(calls) == 1:
            if failure == 'connect':
                raise httpx.ConnectError('down')
            if failure == 'timeout':
                raise httpx.ReadTimeout('slow')  # No idempotency key: a replay could duplicate the message.
            return httpx.Response(429 if failure == 'http_429' else 500, json={'code': 'Throttling', 'message': 'x'})
        return httpx.Response(200, json={'processQueryKey': 'receipt'})
    delivered, _ = wire(handler, ding_db)
    assert len(calls) == expected_calls and delivered == (expected_calls == 2)


def table(n):
    return f'| h{n} | x |\n| --- | --- |\n| {n} | y |\n'


def test_card_markdown_images_become_links():
    assert im.feishu_markdown('看 ![主图](https://img.example/a.png) 与 ![](https://img.example/b.png)') == \
        '看 [主图](https://img.example/a.png) 与 [图片](https://img.example/b.png)'


def test_card_markdown_keeps_five_tables_and_fences_the_rest():
    text = '\n'.join(table(i) for i in range(7))
    result = im.feishu_markdown(text)
    assert result.count('```') == 4  # Tables 6 and 7 are wrapped, 1-5 stay tables.
    assert result.split('```')[0].count('| --- |') == 5
    assert im.feishu_markdown(table(1) + '\n文字\n') == table(1) + '\n文字\n'


def test_card_markdown_ignores_pipes_inside_code_and_closes_a_trailing_fence():
    code = '```\n| a | b |\n```\n'
    assert im.feishu_markdown(code * 8) == code * 8
    trailing = '\n'.join(table(i) for i in range(6)).rstrip('\n')
    fixed = im.feishu_markdown(trailing)
    assert fixed.count('```') == 2 and fixed.endswith('```')
