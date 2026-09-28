"""Authenticated IM ingress and bounded, server-configured reply delivery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from .db import get_db
from .models import Conversation, Group, Identity, IMEvent, User
from . import policy, im_settings

router = APIRouter(prefix='/api/im', tags=['im'])
MAX_BODY = 256 * 1024
WINDOW_SECONDS = 300


def _reject(status=403):
    raise HTTPException(status, 'IM request rejected')


def _same(expected, supplied):
    return isinstance(supplied, str) and hmac.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8'))


def _required(name):
    value = im_settings.value(name, '')
    if not value:
        raise HTTPException(503, 'IM integration unavailable')
    return value


def _fresh(timestamp, milliseconds=False):
    try:
        value = int(timestamp)
        if abs(time.time() - value / (1000 if milliseconds else 1)) > WINDOW_SECONDS:
            _reject()
    except (ValueError, TypeError, OverflowError):
        _reject()


def _object(raw):
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            _reject(400)
        return result
    except (ValueError, TypeError, UnicodeError):
        _reject(400)


def decrypt_feishu(encrypted, key):
    """Official Lark AES layout: base64(16-byte IV + AES-CBC ciphertext)."""
    try:
        data = base64.b64decode(encrypted, validate=True)
        if len(data) < 32 or len(data) % 16:
            _reject()
        decryptor = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(data[:16])).decryptor()
        padded = decryptor.update(data[16:]) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return _object(unpadder.update(padded) + unpadder.finalize())
    except (ValueError, TypeError, UnicodeError):
        _reject()


def verify_feishu(raw, headers):
    payload = _object(raw)
    key = im_settings.value('FEISHU_ENCRYPT_KEY', '')
    if 'encrypt' in payload:
        payload = decrypt_feishu(payload['encrypt'], _required('FEISHU_ENCRYPT_KEY'))
    header = payload.get('header') or {}
    if not isinstance(header, dict):
        _reject(400)
    token = header.get('token') if payload.get('schema') == '2.0' else payload.get('token')
    if not isinstance(token, str) or not _same(_required('FEISHU_VERIFICATION_TOKEN'), token):
        _reject()
    # Official dispatcher answers URL verification before signature verification.
    if payload.get('type') == 'url_verification':
        if not isinstance(payload.get('challenge'), str) or len(payload['challenge']) > 4096:
            _reject(400)
        return payload
    if not key:
        _required('FEISHU_ENCRYPT_KEY')
    timestamp = headers.get('x-lark-request-timestamp', '')
    nonce = headers.get('x-lark-request-nonce', '')
    signature = headers.get('x-lark-signature', '')
    _fresh(timestamp)
    if not nonce or len(nonce) > 256:
        _reject()
    expected = hashlib.sha256((timestamp + nonce + key).encode() + raw).hexdigest()
    if not _same(expected, signature):
        _reject()
    return payload


def verify_dingtalk(headers):
    timestamp = headers.get('timestamp', '')
    _fresh(timestamp, milliseconds=True)
    secret = _required('DINGTALK_APP_SECRET')
    expected = base64.b64encode(hmac.new(secret.encode(), (timestamp + '\n' + secret).encode(), hashlib.sha256).digest()).decode()
    if not _same(expected, headers.get('sign', '')):
        _reject()


def strict_dingtalk_url(url):
    """Only the exact official robot endpoint; never suffix-match arbitrary hosts."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == 'oapi.dingtalk.com'
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and parsed.path == '/robot/send' and not parsed.fragment)
    except ValueError:
        return False


async def _body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            _reject(413)
    return bytes(raw)


def _text(value, limit=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _reject(400)
    return value.strip()


def _lock(db, key):
    # Bound value, stable across processes; serializes duplicate callbacks and chat creation.
    lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big', signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_id)))


def transport(provider):
    mode = im_settings.value(provider.upper() + '_TRANSPORT', 'webhook') or 'webhook'
    if mode not in ('webhook', 'websocket' if provider == 'feishu' else 'stream'):
        raise ValueError('Invalid IM transport')
    return mode


def configuration(provider):
    try:
        mode = transport(provider)
    except ValueError:
        return {'configured': False, 'transport': 'invalid', 'state': 'invalid_config', 'missing': []}
    names = (('FEISHU_APP_ID', 'FEISHU_APP_SECRET') if provider == 'feishu' else
             ('DINGTALK_CLIENT_ID', 'DINGTALK_CLIENT_SECRET', 'DINGTALK_ROBOT_CODE'))
    if mode == 'webhook':
        names = (names + ('FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY') if provider == 'feishu' else
                 ('DINGTALK_APP_SECRET', 'DINGTALK_ROBOT_ACCESS_TOKEN', 'DINGTALK_ROBOT_CHAT_ID'))
    missing = [name for name in names if not im_settings.value(name)]
    return {'configured': not missing, 'transport': mode, 'missing': missing,
            'state': 'missing_credentials' if missing else ('webhook_configured' if mode == 'webhook' else 'connection_unobserved')}


def _enqueue(db, provider, event_id, sender_id, chat_id, content, is_group, reply_mode='webhook', nickname=None, ingress_reason=None):
    from .service import enqueue_message
    from . import im_discovery
    event_id, sender_id, chat_id = (_text(event_id, 256), _text(sender_id, 200), _text(chat_id, 200))
    content = _text(content)
    app_scope = im_discovery.scope(provider)
    # Store only a scoped digest; rejected event tombstones prevent later replay.
    raw_message_id = event_id
    event_id = hashlib.sha256((app_scope + ':' + event_id).encode()).hexdigest()
    _lock(db, 'im:event:' + provider + ':' + event_id)
    if db.scalar(select(IMEvent).where(IMEvent.provider == provider, IMEvent.event_id == event_id)):
        return {'ok': True, 'duplicate': True}
    rejection, user, group = im_discovery.reason(db, provider, app_scope, sender_id, chat_id, is_group)
    rejection = rejection or ingress_reason
    if rejection:
        im_discovery.record(db, provider, app_scope, sender_id, chat_id, is_group, rejection, nickname)
        db.add(IMEvent(provider=provider, event_id=event_id, reply_target={}, delivery_error='NOT_AUTHORIZED'))
        db.flush()
        return {'ok': True, 'pending': True}
    scope = 'group:' + group.id if group else 'user:' + user.id + ':' + chat_id
    _lock(db, 'im:chat:' + provider + ':' + scope)
    # Provider-specific title prevents accidentally reusing a web private conversation.
    title = provider + ':' + hashlib.sha256(scope.encode()).hexdigest()[:32]
    query = select(Conversation).where(Conversation.title == title, Conversation.archived_at.is_(None))
    query = query.where(Conversation.group_id == group.id) if group else query.where(Conversation.owner_id == user.id, Conversation.group_id.is_(None))
    conversation = db.scalar(query.order_by(Conversation.created_at).with_for_update()
                             .execution_options(populate_existing=True))
    if conversation is None:
        conversation = Conversation(title=title, owner_id=user.id, group_id=group.id if group else None)
        db.add(conversation)
        db.flush()
    if not policy.can_read_conversation(db, user, conversation) or not policy.can_send_conversation(db, user, conversation):
        _reject()
    event = IMEvent(provider=provider, event_id=event_id,
                    reply_target={'app_scope': app_scope, 'reply_mode': reply_mode, 'chat_id': chat_id, 'sender_id': sender_id, 'user_id': user.id,
                                  'conversation_id': conversation.id, 'group_id': group.id if group else None})
    db.add(event)
    db.flush()
    result = enqueue_message(db, user, conversation, content)
    run = result['run']
    event.run_id = run['id'] if isinstance(run, dict) else run.id
    if provider == 'feishu':
        from .models import IMReaction
        db.add(IMReaction(event_id=event.id, message_id=raw_message_id, app_scope=app_scope))
    db.flush()
    return {'ok': True}


@router.post('/feishu/callback')
async def feishu_callback(request: Request, db=Depends(get_db)):
    if db is None:  # Direct protocol unit tests.
        return await _feishu_callback(request, db)
    from .im_discovery import configuration_lock
    configuration_lock(db, 'feishu')
    with im_settings.snapshot(db, 'feishu'):
        return await _feishu_callback(request, db)


async def _feishu_callback(request, db):
    if transport('feishu') != 'webhook':
        _reject(404)
    payload = verify_feishu(await _body(request), request.headers)
    if payload.get('type') == 'url_verification':
        return {'challenge': payload['challenge']}
    try:
        header, event = payload['header'], payload['event']
        if header.get('event_type') != 'im.message.receive_v1':
            return {'ok': True, 'ignored': True}
        message, sender = event['message'], event['sender']
        if message.get('message_type') != 'text' or sender.get('sender_type') != 'user':
            return {'ok': True, 'ignored': True}
        if message.get('chat_type') not in ('group', 'p2p'):
            _reject(400)
        return _enqueue(db, 'feishu', message['message_id'], sender['sender_id']['open_id'],
                        message['chat_id'], _object(message['content'])['text'], message['chat_type'] == 'group')
    except (KeyError, TypeError, AttributeError):
        _reject(400)


@router.post('/dingtalk/callback')
async def dingtalk_callback(request: Request, db=Depends(get_db)):
    if db is None:
        return await _dingtalk_callback(request, db)
    from .im_discovery import configuration_lock
    configuration_lock(db, 'dingtalk')
    with im_settings.snapshot(db, 'dingtalk'):
        return await _dingtalk_callback(request, db)


async def _dingtalk_callback(request, db):
    if transport('dingtalk') != 'webhook':
        _reject(404)
    raw = await _body(request)
    verify_dingtalk(request.headers)
    payload = _object(raw)
    try:
        if payload.get('msgtype') != 'text':
            return {'ok': True, 'ignored': True}
        # Fixed webhook belongs to one group; it cannot reply to arbitrary conversations.
        if str(payload.get('conversationType')) not in ('1', '2'):
            _reject(400)
        is_group = str(payload['conversationType']) == '2'
        fixed_chat = _required('DINGTALK_ROBOT_CHAT_ID')
        _required('DINGTALK_ROBOT_ACCESS_TOKEN')
        return _enqueue(db, 'dingtalk', payload['msgId'], payload['senderStaffId'],
                        payload['conversationId'], payload['text']['content'], is_group,
                        nickname=payload.get('senderNick'),
                        ingress_reason=None if is_group and payload['conversationId'] == fixed_chat else 'unsupported_reply_target')
    except (KeyError, TypeError, AttributeError):
        _reject(400)


def _post(client, url, **kwargs):
    response = client.post(url, **kwargs)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('code', data.get('errcode', 0)) != 0:
        raise ValueError('IM API rejected request')
    return data


_token_cache = {}
_token_lock = threading.Lock()


def access_token(client, provider):
    names = ('FEISHU_APP_ID', 'FEISHU_APP_SECRET') if provider == 'feishu' else ('DINGTALK_CLIENT_ID', 'DINGTALK_CLIENT_SECRET')
    credentials = tuple(_required(name) for name in names)
    fingerprint = hashlib.sha256(json.dumps(credentials).encode()).hexdigest()
    key = (provider, fingerprint)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        if provider == 'feishu':
            data = _post(client, 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                         json={'app_id': credentials[0], 'app_secret': credentials[1]})
            token, lifetime = data['tenant_access_token'], data['expire']
        else:
            data = _post(client, 'https://api.dingtalk.com/v1.0/oauth2/accessToken',
                         json={'appKey': credentials[0], 'appSecret': credentials[1]})
            token, lifetime = data['accessToken'], data['expireIn']
        if not isinstance(token, str) or not token or float(lifetime) <= 0:
            raise ValueError('Invalid token response')
        _token_cache.clear() if len(_token_cache) > 8 else None
        _token_cache[key] = (token, time.monotonic() + max(0, float(lifetime) - 60))
        return token


def deliver_reply(db, run, text):
    event = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id))
    if event is None:
        return
    try:
        with im_settings.snapshot(db, event.provider):
            return _deliver_reply(db, run, text)
    except HTTPException:
        event.delivery_error = 'IM_CONFIGURATION_UNAVAILABLE'
        db.flush()


def _deliver_reply(db, run, text):
    """Called by worker after completion; caller commits delivery metadata.

    No automatic retry: a remote send followed by a process crash is ambiguous.
    Errors are reduced to a static code, never the response body or request URL.
    """
    event = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id).with_for_update())
    if event is None or event.delivered_at is not None or event.delivery_error:
        return
    try:
        target = event.reply_target
        from . import im_discovery
        if target.get('app_scope') != im_discovery.scope(event.provider):
            raise ValueError('Application changed')
        rejection, _, _ = im_discovery.reason(db, event.provider, target['app_scope'], target['sender_id'], target['chat_id'], bool(target.get('group_id')))
        if rejection:
            raise ValueError('Mapping no longer authorized')
        user = db.get(User, target['user_id'])
        conversation = db.get(Conversation, target['conversation_id'])
        identity = db.scalar(select(Identity).where(Identity.provider == event.provider,
                             Identity.external_user_id == target['sender_id'], Identity.user_id == target['user_id']))
        if (not identity or not user or not conversation or conversation.group_id != target.get('group_id')
                or not policy.can_send_conversation(db, user, conversation)):
            raise ValueError('Delivery no longer authorized')
        if target.get('group_id'):
            group = db.get(Group, target['group_id'])
            if not group or group.provider != event.provider or group.external_id != target['chat_id']:
                raise ValueError('Chat mapping changed')
        text = _text(text, 100000)[:1800]
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            if event.provider == 'feishu':
                token = access_token(client, 'feishu')
                _post(client, 'https://open.feishu.cn/open-apis/im/v1/messages', params={'receive_id_type': 'chat_id'},
                      headers={'Authorization': 'Bearer ' + token}, json={'receive_id': target['chat_id'],
                      'msg_type': 'text', 'content': json.dumps({'text': text}), 'uuid': event.id})
            elif event.provider == 'dingtalk' and target.get('reply_mode', 'webhook') == 'stream':
                token = access_token(client, 'dingtalk')
                body = {'robotCode': _required('DINGTALK_ROBOT_CODE'), 'msgKey': 'sampleText',
                        'msgParam': json.dumps({'content': text}, ensure_ascii=False)}
                if target.get('group_id'):
                    endpoint = 'groupMessages/send'
                    body['openConversationId'] = target['chat_id']
                else:
                    endpoint = 'oToMessages/batchSend'
                    body['userIds'] = [target['sender_id']]
                data = _post(client, 'https://api.dingtalk.com/v1.0/robot/' + endpoint,
                             headers={'x-acs-dingtalk-access-token': token}, json=body)
                if data.get('invalidStaffIdList') or data.get('flowControlledStaffIdList') or data.get('filteredStaffIdList') or not data.get('processQueryKey'):
                    raise ValueError('IM API rejected recipients')
            elif event.provider == 'dingtalk':
                if target.get('reply_mode', 'webhook') != 'webhook':
                    raise ValueError('Unknown delivery mode')
                if target['chat_id'] != _required('DINGTALK_ROBOT_CHAT_ID'):
                    raise ValueError('Chat mapping changed')
                url = 'https://oapi.dingtalk.com/robot/send'
                if not strict_dingtalk_url(url):
                    raise ValueError('Invalid endpoint')
                params = {'access_token': _required('DINGTALK_ROBOT_ACCESS_TOKEN')}
                secret = im_settings.value('DINGTALK_ROBOT_SECRET', '')
                if secret:
                    timestamp = str(int(time.time() * 1000))
                    params.update(timestamp=timestamp, sign=base64.b64encode(hmac.new(secret.encode(),
                                  (timestamp + '\n' + secret).encode(), hashlib.sha256).digest()).decode())
                _post(client, url, params=params, json={'msgtype': 'text', 'text': {'content': text}})
            else:
                raise ValueError('Unknown provider')
        event.delivered_at = datetime.now(timezone.utc)
    except Exception:
        event.delivery_error = 'IM_DELIVERY_FAILED'
    db.flush()
