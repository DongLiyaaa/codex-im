"""Actual SDK parsing + isolated PostgreSQL transactions + mocked outbound HTTP."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from test_im_postgres import database
from app import im, im_connections as connections
from app.models import User, Group, Identity, IMEvent, Run, Conversation, IMScopeBinding
from app.im_discovery import scope


def feishu(sender='sender', chat='chat', kind='group'):
    return {'header': {'event_type': 'im.message.receive_v1'}, 'event': {
        'sender': {'sender_type': 'user', 'sender_id': {'open_id': sender}},
        'message': {'message_id': 'message1', 'chat_id': chat, 'chat_type': kind,
                    'message_type': 'text', 'content': json.dumps({'text': 'hello'})}}}


def ding(sender='sender', chat='chat', kind='2'):
    return {'msgId': 'message1', 'msgtype': 'text', 'text': {'content': 'hello'},
            'senderStaffId': sender, 'conversationId': chat, 'conversationType': kind,
            'robotCode': 'robot', 'sessionWebhook': 'https://untrusted.invalid/secret'}


@pytest.fixture
def ding_db(database, monkeypatch):
    monkeypatch.setenv('DINGTALK_ROBOT_CODE', 'robot')
    with database.begin() as db:
        db.scalar(select(Group)).provider = 'dingtalk'
        db.scalar(select(Identity)).provider = 'dingtalk'
        for binding in db.scalars(select(IMScopeBinding)):
            binding.app_scope = scope('dingtalk')
    return database


@pytest.mark.parametrize('provider', ['feishu', 'dingtalk'])
def test_sdk_adapter_concurrent_dedup(database, monkeypatch, provider):
    monkeypatch.setenv('DINGTALK_ROBOT_CODE', 'robot')
    with database.begin() as db:
        db.scalar(select(Group)).provider = provider
        db.scalar(select(Identity)).provider = provider
        for binding in db.scalars(select(IMScopeBinding)):
            binding.app_scope = scope(provider)
    receiver, payload = (connections.receive_feishu, feishu()) if provider == 'feishu' else (connections.receive_dingtalk, ding())
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: receiver(payload, database), range(3)))
    assert sum(bool(x.get('duplicate')) for x in results) == 2
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        event = db.scalar(select(IMEvent))
        assert event.reply_target['reply_mode'] == ('websocket' if provider == 'feishu' else 'stream')
        assert 'sessionWebhook' not in json.dumps(event.reply_target)


@pytest.mark.parametrize('kind', ['1', '2'])
def test_ding_private_and_group(ding_db, kind):
    assert connections.receive_dingtalk(ding(kind=kind), ding_db) == {'ok': True}


@pytest.mark.parametrize('provider', ['feishu', 'dingtalk'])
@pytest.mark.parametrize('failure', ['unknown', 'group', 'inactive', 'membership', 'org', 'rollback'])
def test_adapter_denial_and_rollback(database, monkeypatch, provider, failure):
    monkeypatch.setenv('DINGTALK_ROBOT_CODE', 'robot')
    with database.begin() as db:
        db.scalar(select(Group)).provider = provider
        db.scalar(select(Identity)).provider = provider
        for binding in db.scalars(select(IMScopeBinding)):
            binding.app_scope = scope(provider)
        if failure == 'inactive':
            db.scalar(select(User)).active = False
        if failure == 'membership':
            db.scalar(select(Group)).member_ids = []
        if failure == 'org':
            db.scalar(select(User)).org_id = 'other'
    if failure == 'rollback':
        def fail(*args):
            raise RuntimeError('synthetic')
        monkeypatch.setattr('app.service.enqueue_message', fail)
    payload = (feishu if provider == 'feishu' else ding)(sender='unknown' if failure == 'unknown' else 'sender', chat='other' if failure == 'group' else 'chat')
    receiver = connections.receive_feishu if provider == 'feishu' else connections.receive_dingtalk
    if failure == 'rollback':
        with pytest.raises(RuntimeError):
            receiver(payload, database)
    else:
        assert receiver(payload, database)['pending']
    with database.begin() as db:
        for model in (Run, Conversation):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(IMEvent)) == (0 if failure == 'rollback' else 1)


@pytest.mark.parametrize('kind', ['1', '2'])
@pytest.mark.parametrize('error', ['', 'invalidStaffIdList', 'flowControlledStaffIdList', 'filteredStaffIdList', 'http', 'code', 'missing_receipt', 'revoked'])
def test_ding_outbound(ding_db, monkeypatch, kind, error):
    connections.receive_dingtalk(ding(kind=kind), ding_db)
    monkeypatch.setenv('DINGTALK_CLIENT_ID', 'app')
    monkeypatch.setenv('DINGTALK_CLIENT_SECRET', 'secret')
    im._token_cache.clear()
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith('/accessToken'):
            assert body == {'appKey': 'app', 'appSecret': 'secret'}
            return httpx.Response(200, json={'accessToken': 'test-token', 'expireIn': 7200})
        assert request.headers['x-acs-dingtalk-access-token'] == 'test-token'
        assert body['robotCode'] == 'robot' and body['msgKey'] == 'sampleText'
        assert json.loads(body['msgParam']) == {'content': 'reply'}
        if kind == '2':
            assert request.url.path == '/v1.0/robot/groupMessages/send'
            assert body['openConversationId'] == 'chat' and 'userIds' not in body
        else:
            assert request.url.path == '/v1.0/robot/oToMessages/batchSend'
            assert body['userIds'] == ['sender'] and 'openConversationId' not in body
        result = {'processQueryKey': 'receipt'}
        if error in ('invalidStaffIdList', 'flowControlledStaffIdList', 'filteredStaffIdList'):
            result[error] = ['sender']
        if error == 'code':
            result['code'] = 'Forbidden'
        if error == 'missing_receipt':
            result = {}
        return httpx.Response(403 if error == 'http' else 200, json=result)
    real_client = httpx.Client
    monkeypatch.setattr(im.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with ding_db.begin() as db:
        if error == 'revoked':
            db.scalar(select(User)).active = False
        event, run = db.scalar(select(IMEvent)), db.scalar(select(Run))
        im.deliver_reply(db, run, 'reply')
        assert bool(event.delivery_error) == bool(error)
        assert bool(event.delivered_at) == (not error)
        if error:
            assert event.delivery_error == 'IM_DELIVERY_FAILED'
        im.deliver_reply(db, run, 'reply')
    assert len(calls) == (0 if error == 'revoked' else 2)


def test_token_cache_and_rotation(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    im._token_cache.clear()
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert im.access_token(client, 'feishu') == im.access_token(client, 'feishu')
        assert len(calls) == 1
        monkeypatch.setenv('FEISHU_APP_SECRET', 'rotated')
        im.access_token(client, 'feishu')
        assert len(calls) == 2


def test_no_credentials_no_sdk_connection(monkeypatch):
    for provider, mode in [('feishu', 'websocket'), ('dingtalk', 'stream')]:
        for name in list(__import__('os').environ):
            if name.startswith(provider.upper() + '_'):
                monkeypatch.delenv(name)
        monkeypatch.setenv(provider.upper() + '_TRANSPORT', mode)
        monkeypatch.setattr(connections, 'record_state', lambda *args: pytest.fail('must not connect'))
        connections.run(provider)
        assert im.configuration(provider)['state'] == 'missing_credentials'


def test_sdk_contract():
    import lark_oapi as lark
    import dingtalk_stream as ding_sdk
    event = lark.api.im.v1.P2ImMessageReceiveV1(feishu())
    assert json.loads(lark.JSON.marshal(event))['event']['message']['message_id'] == 'message1'
    assert ding_sdk.ChatbotMessage.TOPIC == '/v1.0/im/bot/messages/get'
    parsed = ding_sdk.ChatbotMessage.from_dict(ding())
    assert parsed.sender_staff_id == 'sender' and parsed.conversation_id == 'chat'
    assert not connections.socket_open(None)
    assert connections.socket_open(SimpleNamespace(state=SimpleNamespace(name='OPEN')))
    assert not connections.socket_open(SimpleNamespace(state=SimpleNamespace(name='CLOSED')))


def test_ack_transient_failure_and_permanent_denial():
    for status, expected in [(400, 200), (403, 200), (409, 500), (503, 500)]:
        def fail(payload):
            raise HTTPException(status)
        assert connections.safe_receive(fail, {}) == expected


def test_feishu_outbound(database, monkeypatch):
    connections.receive_feishu(feishu(kind='p2p'), database)
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    im._token_cache.clear()
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        if 'tenant_access_token' in request.url.path:
            assert body == {'app_id': 'app', 'app_secret': 'secret'}
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
        assert request.url.params['receive_id_type'] == 'chat_id'
        assert request.headers['authorization'] == 'Bearer token'
        assert body['receive_id'] == 'chat' and body['msg_type'] == 'text'
        assert json.loads(body['content']) == {'text': 'reply'}
        assert body['uuid']
        return httpx.Response(200, json={'code': 0})
    real_client = httpx.Client
    monkeypatch.setattr(im.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with database.begin() as db:
        im.deliver_reply(db, db.scalar(select(Run)), 'reply')
        assert db.scalar(select(IMEvent)).delivered_at
    assert len(calls) == 2


def test_connection_status_stale(database, monkeypatch):
    from datetime import timedelta
    from app.models import IMConnection, now
    from app.main import integrations
    monkeypatch.setenv('FEISHU_TRANSPORT', 'websocket')
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'secret')
    with database.begin() as db:
        assert integrations(None, db)['feishu']['state'] == 'connection_unobserved'
        from app import im_settings
        values, _ = im_settings.effective(db, 'feishu')
        row = IMConnection(provider='feishu', transport='websocket', state='connected:' + im_settings.fingerprint(values), updated_at=now())
        db.add(row)
        db.flush()
        assert integrations(None, db)['feishu']['state'] == 'connected'
        row.updated_at = now() - timedelta(seconds=30)
        assert integrations(None, db)['feishu']['state'] == 'stale'


def test_webhook_routes_disabled_in_long_connection_mode(monkeypatch):
    monkeypatch.setenv('FEISHU_TRANSPORT', 'websocket')
    monkeypatch.setenv('DINGTALK_TRANSPORT', 'stream')
    for callback in (im.feishu_callback, im.dingtalk_callback):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(callback(None, None))
        assert exc.value.status_code == 404
