"""Admin-triggered Feishu sender-nickname and group-name enrichment for discoveries.

Feishu message events carry only sender and chat IDs. Lookups run only when a super
admin refreshes discoveries (never on ingress, so unknown senders cannot drive
outbound API usage), outside any open DB transaction, and only fill empty values
that still belong to the current application scope. DingTalk events already carry
senderNick and conversationTitle, so no DingTalk API call is made here.
"""
import re
import threading
import time
from collections import Counter
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select

from . import im_settings
from .models import IMDiscovery, IMChatName

BASE = 'https://open.feishu.cn/open-apis'
BATCH = 20
MEMBER_PAGES = 5
DEADLINE = 10.0
OPEN_ID = re.compile(r'ou_[A-Za-z0-9_-]{8,64}')
CHAT_ID = re.compile(r'oc_[A-Za-z0-9_-]{8,64}')
CHAT_KEY = 'chat:'
# Platform error codes from the official contact, chat and chat-member API documentation.
CODES = {41050: 'outside_contact_scope', 99991672: 'permission_missing', 99991679: 'permission_missing',
         232025: 'permission_missing', 232034: 'permission_missing', 232011: 'bot_not_in_chat',
         232010: 'external_chat', 232033: 'external_chat', 232006: 'invalid_id'}
# Configuration problems are retried hourly; transient failures after five minutes.
TTL = {'lookup_failed': 300}
DEFAULT_TTL = 3600
_failures = {}
_failures_lock = threading.Lock()
_refresh_lock = threading.Lock()


def clean(name):
    if not isinstance(name, str):
        return None
    name = ''.join(c for c in name if c.isprintable()).strip()
    return name[:200] or None


def failure(app_scope, key):
    with _failures_lock:
        entry = _failures.get((app_scope, key))
        if entry and entry[1] > time.monotonic():
            return entry[0]
        _failures.pop((app_scope, key), None)
    return None


def chat_failure(app_scope, chat):
    return failure(app_scope, CHAT_KEY + chat)


def _remember(app_scope, key, code):
    with _failures_lock:
        if len(_failures) > 5000:
            _failures.clear()
        _failures[(app_scope, key)] = (code, time.monotonic() + TTL.get(code, DEFAULT_TTL))


def _get(client, token, path, params):
    try:
        response = client.get(BASE + path, params=params, headers={'Authorization': 'Bearer ' + token})
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None, 'lookup_failed'
    if not isinstance(data, dict):
        return None, 'lookup_failed'
    if response.status_code == 200 and data.get('code') == 0:
        payload = data.get('data')
        return payload if isinstance(payload, dict) else {}, None
    return None, CODES.get(data.get('code'), 'lookup_failed')


def contact_name(client, token, sender):
    data, error = _get(client, token, '/contact/v3/users/' + quote(sender, safe=''), {'user_id_type': 'open_id'})
    if error:
        return None, error
    user = data.get('user') if isinstance(data.get('user'), dict) else {}
    name = clean(user.get('name')) or clean(user.get('nickname')) or clean(user.get('en_name'))
    return (name, None) if name else (None, 'name_unavailable')


def chat_title(client, token, chat):
    data, error = _get(client, token, '/im/v1/chats/' + quote(chat, safe=''), {})
    if error:
        return None, error
    names = data.get('i18n_names') if isinstance(data.get('i18n_names'), dict) else {}
    name = clean(data.get('name')) or clean(names.get('zh_cn')) or clean(names.get('en_us'))
    return (name, None) if name else (None, 'name_unavailable')


def chat_members(client, token, chat, deadline):
    names, page_token = {}, None
    for _ in range(MEMBER_PAGES):
        if time.monotonic() > deadline:
            return names, 'lookup_failed'
        params = {'member_id_type': 'open_id', 'page_size': 100}
        if page_token:
            params['page_token'] = page_token
        data, error = _get(client, token, '/im/v1/chats/' + quote(chat, safe='') + '/members', params)
        if error:
            return names, error
        for item in data.get('items') or []:
            if isinstance(item, dict) and isinstance(item.get('member_id'), str) and clean(item.get('name')):
                names[item['member_id']] = clean(item.get('name'))
        page_token = data.get('page_token')
        if not data.get('has_more') or not isinstance(page_token, str) or not page_token:
            break
    return names, None


def _client():
    return httpx.Client(timeout=3, follow_redirects=False, trust_env=False)


def _summary(status, users=None, chats=None):
    result = {'status': status}
    for prefix, part in (('', users), ('chat_', chats)):
        resolved, failed, remaining, cached = part or ({}, {}, 0, 0)
        result.update({prefix + 'resolved': len(resolved), prefix + 'unresolved': len(failed),
                       prefix + 'remaining': remaining, prefix + 'cached': cached,
                       prefix + 'reasons': dict(Counter(failed.values()))})
    return result


def _missing(app_scope):
    return (select(IMDiscovery).where(IMDiscovery.provider == 'feishu', IMDiscovery.app_scope == app_scope,
                                      IMDiscovery.nickname.is_(None)))


def _unnamed_chats(app_scope):
    named = select(IMChatName.chat_id).where(IMChatName.provider == 'feishu', IMChatName.app_scope == app_scope,
                                             IMChatName.chat_id == IMDiscovery.chat_id).exists()
    return (select(IMDiscovery.chat_id).where(IMDiscovery.provider == 'feishu', IMDiscovery.app_scope == app_scope,
                                              IMDiscovery.chat_type == 'group', ~named)
            .group_by(IMDiscovery.chat_id).order_by(func.max(IMDiscovery.last_seen).desc()).limit(500))


def refresh(db, client_factory=None):
    if not _refresh_lock.acquire(blocking=False):
        return _summary('busy')
    try:
        return _refresh(db, client_factory or _client)
    finally:
        _refresh_lock.release()


def _lookup(client_factory, pending, chats):
    from . import im
    resolved, failed, attempted, members = {}, {}, 0, {}
    chat_resolved, chat_failed, chat_attempted = {}, {}, 0
    deadline = time.monotonic() + DEADLINE
    with client_factory() as client:
        token = im.access_token(client, 'feishu')
        for sender, sender_chats in pending[:BATCH]:
            if time.monotonic() > deadline:
                break
            attempted += 1
            if not OPEN_ID.fullmatch(sender):
                failed[sender] = 'invalid_id'
                continue
            name, error = contact_name(client, token, sender)
            for chat in [] if name else sender_chats:
                if not CHAT_ID.fullmatch(chat):
                    continue
                if chat not in members:
                    members[chat] = chat_members(client, token, chat, deadline)[0]
                name = members[chat].get(sender)
                if name:
                    break
            if name:
                resolved[sender] = name
            else:
                failed[sender] = error
        for chat in chats[:BATCH]:
            if time.monotonic() > deadline:
                break
            chat_attempted += 1
            if not CHAT_ID.fullmatch(chat):
                chat_failed[chat] = 'invalid_id'
                continue
            name, error = chat_title(client, token, chat)
            if name:
                chat_resolved[chat] = name
            else:
                chat_failed[chat] = error
    return (resolved, failed, attempted), (chat_resolved, chat_failed, chat_attempted)


def _refresh(db, client_factory):
    from .im_discovery import scope, configuration_lock, store_chat_names
    with im_settings.snapshot(db, 'feishu'):
        try:
            app_scope = scope('feishu')
        except HTTPException:
            return _summary('unconfigured')
        if not im_settings.value('FEISHU_APP_SECRET'):
            return _summary('unconfigured')
        rows = list(db.scalars(_missing(app_scope).order_by(IMDiscovery.last_seen.desc()).limit(500)))
        unnamed = list(db.scalars(_unnamed_chats(app_scope)))
        # End the read transaction before any outbound request.
        db.commit()
        senders, cached = {}, 0
        for row in rows:
            if row.sender_id not in senders and failure(app_scope, row.sender_id):
                cached += 1
                senders[row.sender_id] = None
            sender_chats = senders.setdefault(row.sender_id, [])
            if sender_chats is not None and row.chat_type == 'group' and row.chat_id not in sender_chats:
                sender_chats.append(row.chat_id)
        pending = [(sender, chats) for sender, chats in senders.items() if chats is not None]
        chats = [chat for chat in unnamed if not chat_failure(app_scope, chat)]
        chat_cached = len(unnamed) - len(chats)
        users, groups = ({}, {}, 0), ({}, {}, 0)
        if pending or chats:
            try:
                users, groups = _lookup(client_factory, pending, chats)
            except (HTTPException, httpx.HTTPError, ValueError, KeyError, TypeError):
                return _summary('token_failed', ({}, {}, len(pending), cached), ({}, {}, len(chats), chat_cached))
    configuration_lock(db, 'feishu')
    with im_settings.snapshot(db, 'feishu'):
        try:
            current = scope('feishu')
        except HTTPException:
            current = None
    if current != app_scope:
        return _summary('application_changed', ({}, {}, len(pending), cached), ({}, {}, len(chats), chat_cached))
    resolved, failed, attempted = users
    chat_resolved, chat_failed, chat_attempted = groups
    if resolved:
        for row in db.scalars(_missing(app_scope).where(IMDiscovery.sender_id.in_(list(resolved))).with_for_update()):
            row.nickname = resolved[row.sender_id]
        db.flush()
    if chat_resolved:
        store_chat_names(db, 'feishu', app_scope, chat_resolved)
    for sender, code in failed.items():
        _remember(app_scope, sender, code)
    for chat, code in chat_failed.items():
        _remember(app_scope, CHAT_KEY + chat, code)
    return _summary('ok', (resolved, failed, len(pending) - attempted, cached),
                    (chat_resolved, chat_failed, len(chats) - chat_attempted, chat_cached))
