"""Authenticated IM ingress and bounded, server-configured reply delivery."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

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


BUSY = '上一个任务还在处理中，这条消息没有执行。请等它完成，或发送 /stop 停止后再重发。'
ACTIVE_RUNS = ('queued', 'running', 'waiting_attachments')


def _enqueue(db, provider, event_id, sender_id, chat_id, content, is_group, reply_mode='webhook', nickname=None, ingress_reason=None, attachment_refs=None, chat_name=None, notice=None, sender_internal=False):
    from .service import enqueue_message
    from . import im_discovery
    from .models import IMOutbox, Run
    event_id, sender_id, chat_id = (_text(event_id, 256), _text(sender_id, 200), _text(chat_id, 200))
    content = '' if notice else (_text(content) if content or not attachment_refs else '')
    app_scope = im_discovery.scope(provider)
    # Store only a scoped digest; rejected event tombstones prevent later replay.
    raw_message_id = event_id
    event_id = hashlib.sha256((app_scope + ':' + event_id).encode()).hexdigest()
    _lock(db, 'im:event:' + provider + ':' + event_id)
    if db.scalar(select(IMEvent).where(IMEvent.provider == provider, IMEvent.event_id == event_id)):
        return {'ok': True, 'duplicate': True}
    rejection, user, group = im_discovery.reason(db, provider, app_scope, sender_id, chat_id, is_group)
    if rejection == 'unknown_sender' and sender_internal and not is_group and not ingress_reason:
        # Opt-in policy only; any refusal or failure leaves the sender in the discovery list as before.
        from . import im_onboarding
        if im_onboarding.auto_onboard(db, provider, app_scope, sender_id, nickname):
            rejection, user, group = im_discovery.reason(db, provider, app_scope, sender_id, chat_id, is_group)
    rejection = rejection or ingress_reason
    if rejection:
        im_discovery.record(db, provider, app_scope, sender_id, chat_id, is_group, rejection, nickname, chat_name)
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
                    reply_target={'app_scope': app_scope, 'reply_mode': reply_mode, 'chat_type': 'group' if is_group else 'p2p', 'chat_id': chat_id, 'sender_id': sender_id, 'user_id': user.id,
                                  'conversation_id': conversation.id, 'group_id': group.id if group else None,
                                  'message_id': raw_message_id})
    db.add(event)
    db.flush()
    if notice:
        # Only authorized senders get here: tell them why the message cannot be handled instead of staying silent.
        db.add(IMOutbox(event_id=event.id, text=notice))
        db.flush()
        return {'ok': True, 'notice': True}
    from . import approvals, im_commands
    busy = bool(db.scalar(select(Run.id).where(Run.conversation_id == conversation.id, Run.status.in_(ACTIVE_RUNS)).limit(1)))
    decision = None if attachment_refs else approvals.parse(content)
    if decision:
        # Only this user's own message can release a risky action; the model never sees or forges it.
        handled = approvals.handle(db, user, conversation, decision, busy)
        if handled.continuation is None:
            db.add(IMOutbox(event_id=event.id, text=handled.reply))
            db.flush()
            return {'ok': True, 'approval': decision.verb}
        content, busy = handled.continuation, False
    else:
        command = None if attachment_refs else im_commands.parse(content)
        if command:
            im_commands.handle(db, command, provider, event, user, group, conversation)
            return {'ok': True, 'command': command}
    if busy:
        # Without this the ingress failed with 409, the user saw nothing and the platform redelivered the event later.
        db.add(IMOutbox(event_id=event.id, text=BUSY))
        db.flush()
        return {'ok': True, 'busy': True}
    if attachment_refs:
        from .attachment_ingress import register
        identifiers = register(db, user, conversation, provider, app_scope, attachment_refs)
        result = enqueue_message(db, user, conversation, content, identifiers)
    else:
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
        if sender.get('sender_type') != 'user':
            return {'ok': True, 'ignored': True}
        if message.get('chat_type') not in ('group', 'p2p'):
            _reject(400)
        import asyncio
        from . import im_inbound
        # Bot identity and quoted-message lookups are network calls: keep them off the event loop.
        inbound = await asyncio.to_thread(im_inbound.feishu, message)
        if inbound is None:
            return {'ok': True, 'ignored': True}
        options = {'attachment_refs': inbound.refs} if inbound.refs else {}
        return _enqueue(db, 'feishu', message['message_id'], sender['sender_id']['open_id'],
                        message['chat_id'], inbound.content, message['chat_type'] == 'group', notice=inbound.notice,
                        sender_internal=im_inbound.feishu_internal(header, sender), **options)
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
        from . import im_inbound
        inbound = im_inbound.dingtalk(payload)
        if inbound is None:
            return {'ok': True, 'ignored': True}
        content, refs = inbound.content, inbound.refs
        # Fixed webhook belongs to one group; it cannot reply to arbitrary conversations.
        if str(payload.get('conversationType')) not in ('1', '2'):
            _reject(400)
        is_group = str(payload['conversationType']) == '2'
        fixed_chat = _required('DINGTALK_ROBOT_CHAT_ID')
        _required('DINGTALK_ROBOT_ACCESS_TOKEN')
        return _enqueue(db, 'dingtalk', payload['msgId'], payload['senderStaffId'],
                        payload['conversationId'], content, is_group,
                        nickname=payload.get('senderNick'), chat_name=payload.get('conversationTitle'), notice=inbound.notice,
                        sender_internal=im_inbound.dingtalk_internal(payload),
                        ingress_reason=None if is_group and payload['conversationId'] == fixed_chat else 'unsupported_reply_target',
                        **({'attachment_refs': refs} if refs else {}))
    except (KeyError, TypeError, AttributeError):
        _reject(400)


class IMAPIError(ValueError):
    """The platform answered with a business error. Stays a ValueError so existing fallbacks keep working."""

    def __init__(self, code=None, message='', status=200):
        super().__init__('IM API rejected request')
        self.code, self.status = code, status
        self.platform_message = str(message or '')[:200]  # Only for classification; never logged or returned.


def _post(client, url, **kwargs):
    response = client.post(url, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = None
    code = data.get('code', data.get('errcode', 0)) if isinstance(data, dict) else None
    status = getattr(response, 'status_code', 200)  # The broker's pinned-HTTPS shim only exposes json().
    if not 200 <= status < 300:
        # Feishu reports business failures (withdrawn message, rejected card...) as HTTP 400 plus a JSON code.
        if isinstance(data, dict) and code not in (None, 0) and status < 500:
            raise IMAPIError(code, data.get('msg') or data.get('message'), status)
        response.raise_for_status()
    if not isinstance(data, dict) or code != 0:
        raise IMAPIError(code, data.get('msg') if isinstance(data, dict) else '', status)
    return data


RETRY_DELAYS = (0.5, 1.5)
RATE_LIMIT_CODES = {99991400, 230020}
TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}
WITHDRAWN_CODES = {230011, 99992354}  # The message we reply to was recalled/deleted.


def _retryable(exc, idempotent):
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True  # The request never reached the platform.
    if isinstance(exc, IMAPIError):
        return exc.status == 429 or exc.code in RATE_LIMIT_CODES
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or (idempotent and exc.response.status_code >= 500)
    if isinstance(exc, httpx.TransportError):
        return idempotent  # Timeout/reset after sending is ambiguous: only safe when the send is idempotent.
    return False


def _send_with_retry(operation, idempotent, provider):
    """Retries transient failures. Feishu sends carry a uuid, so a replay returns the original message."""
    refreshed, attempt = False, 0
    while True:
        try:
            return operation()
        except Exception as exc:
            if isinstance(exc, IMAPIError) and exc.code in TOKEN_INVALID_CODES and provider == 'feishu' and not refreshed:
                refreshed = True
                forget_token(provider)
                continue
            if attempt >= len(RETRY_DELAYS) or not _retryable(exc, idempotent):
                raise
            time.sleep(RETRY_DELAYS[attempt])
            attempt += 1


_token_cache = {}
_token_lock = threading.Lock()


def forget_token(provider):
    # A token the platform no longer accepts (rotated secret, early revocation) must not be served from cache again.
    with _token_lock:
        for key in [key for key in _token_cache if key[0] == provider]:
            del _token_cache[key]


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


SEGMENT_SIZE = 3000
SEGMENT_LIMIT = 8
_MENTION = re.compile(r'<(\s*/?\s*at\b)', re.IGNORECASE)


def sanitize(text):
    # Model output may be steered by group members; never let it produce platform @mentions (e.g. @all).
    return _MENTION.sub('<\u200b\\1', text)


def reply_segments(text, size=SEGMENT_SIZE, limit=SEGMENT_LIMIT):
    rest, parts = sanitize(_text(text, 100000)), []
    while rest:
        if len(rest) <= size:
            parts.append(rest)
            break
        cut = rest.rfind('\n\n', 0, size)
        if cut < size // 2:
            cut = rest.rfind('\n', 0, size)
        if cut < size // 2:
            cut = size
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip('\n')
    truncated = len(parts) > limit
    parts, fenced, result = parts[:limit], False, []
    for part in parts:
        # Keep Markdown code fences balanced across segment boundaries.
        part = ('```\n' + part) if fenced else part
        fenced = part.count('```') % 2 == 1
        result.append(part + ('\n```' if fenced else ''))
    if truncated:
        result[-1] += '\n\n…（回复过长已截断，完整内容请在 Hub 网页会话中查看）'
    if len(result) > 1:
        result = [f'{part}\n\n（{index}/{len(result)}）' for index, part in enumerate(result, 1)]
    return result


def _title(segment):
    line = next((l.strip(' #*>-`') for l in segment.splitlines() if l.strip(' #*>-`')), '')
    return line[:20] or '回复'


FEISHU_MESSAGES = 'https://open.feishu.cn/open-apis/im/v1/messages'
CARD_TABLE_LIMIT = 5  # Feishu rejects a card with more tables (230099 / 11310).
_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)[^)]*\)')


def _is_table_line(line):
    stripped = line.strip()
    return len(stripped) > 1 and stripped[0] == '|' and stripped[-1] == '|'


def feishu_markdown(text):
    """Makes Markdown safe for a Feishu card: external images and surplus tables are rejected by the platform."""
    # The bot never uploads images, so every image reference would be an invalid image key.
    text = _IMAGE.sub(lambda m: f'[{m.group(1) or "图片"}]({m.group(2)})', text)
    result, tables, in_table, in_fence, fenced_table = [], 0, False, False, False
    for line in text.split('\n'):
        if line.strip().startswith('```'):
            in_fence = not in_fence
        table = not in_fence and _is_table_line(line)
        if table and not in_table:
            tables += 1
            fenced_table = tables > CARD_TABLE_LIMIT
            if fenced_table:
                result.append('```')
        elif not table and in_table and fenced_table:
            result.append('```')
            fenced_table = False
        in_table = table
        result.append(line)
    if in_table and fenced_table:
        result.append('```')
    return '\n'.join(result)


def _feishu_post(client, event, target, msg_type, content, uuid):
    # Group answers are anchored to the question; a recalled/deleted original falls back to a plain chat message.
    anchor = target.get('message_id') if target.get('chat_type') == 'group' else None

    def call(reply_to):
        headers = {'Authorization': 'Bearer ' + access_token(client, 'feishu')}
        body = {'msg_type': msg_type, 'content': content, 'uuid': uuid}
        if reply_to:
            return _post(client, f'{FEISHU_MESSAGES}/{quote(reply_to, safe="")}/reply', headers=headers, json=body)
        return _post(client, FEISHU_MESSAGES, params={'receive_id_type': 'chat_id'}, headers=headers,
                     json={**body, 'receive_id': target['chat_id']})
    try:
        return _send_with_retry(lambda: call(anchor), True, 'feishu')
    except IMAPIError as exc:
        if not anchor or exc.code not in WITHDRAWN_CODES:
            raise
        return _send_with_retry(lambda: call(None), True, 'feishu')


def _send_segment(client, event, target, segment, index):
    if event.provider == 'feishu':
        card = {'schema': '2.0', 'body': {'elements': [{'tag': 'markdown', 'content': feishu_markdown(segment)}]}}
        try:
            _feishu_post(client, event, target, 'interactive', json.dumps(card, ensure_ascii=False), f'{event.id}-{index}')
        except IMAPIError:
            # A business-rejected card was not sent; fall back to plain text for this segment only.
            _feishu_post(client, event, target, 'text', json.dumps({'text': segment}), f'{event.id}-t{index}')
    elif event.provider == 'dingtalk' and target.get('reply_mode', 'webhook') == 'stream':
        body = {'robotCode': _required('DINGTALK_ROBOT_CODE'), 'msgKey': 'sampleMarkdown',
                'msgParam': json.dumps({'title': _title(segment), 'text': segment}, ensure_ascii=False)}
        if target.get('group_id'):
            endpoint = 'groupMessages/send'
            body['openConversationId'] = target['chat_id']
        else:
            endpoint = 'oToMessages/batchSend'
            body['userIds'] = [target['sender_id']]
        # DingTalk sends have no idempotency key: only retry when the request provably never arrived (or was throttled).
        data = _send_with_retry(lambda: _post(client, 'https://api.dingtalk.com/v1.0/robot/' + endpoint,
                                              headers={'x-acs-dingtalk-access-token': access_token(client, 'dingtalk')},
                                              json=body), False, 'dingtalk')
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
        _post(client, url, params=params, json={'msgtype': 'text', 'text': {'content': segment}})
    else:
        raise ValueError('Unknown provider')


def deliver_event(db, event, text):
    """Outbox entry point for command replies; same checks and transport as run replies."""
    try:
        with im_settings.snapshot(db, event.provider):
            return _deliver_event(db, event, text)
    except HTTPException:
        event.delivery_error = 'IM_CONFIGURATION_UNAVAILABLE'
        db.flush()


def _deliver_reply(db, run, text):
    """Called by worker after completion; caller commits delivery metadata.

    No automatic retry: a remote send followed by a process crash is ambiguous.
    Errors are reduced to a static code, never the response body or request URL.
    """
    return _deliver_event(db, db.scalar(select(IMEvent).where(IMEvent.run_id == run.id).with_for_update()), text)


def _deliver_event(db, event, text):
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
        segments = reply_segments(text)
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            for index, segment in enumerate(segments):
                _send_segment(client, event, target, segment, index)
        event.delivered_at = datetime.now(timezone.utc)
    except Exception:
        event.delivery_error = 'IM_DELIVERY_FAILED'
    db.flush()
