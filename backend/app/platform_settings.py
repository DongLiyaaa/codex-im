"""Encrypted OAuth configuration; bot reuse is an explicit, revisioned copy."""
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from .models import PlatformSettings
from . import im_settings

from .platform_auth import SCOPES
_active = ContextVar('platform_settings', default=None)


def effective(db, provider):
    if provider not in SCOPES:
        raise HTTPException(404, 'Unknown platform')
    row = db.get(PlatformSettings, provider)
    try:
        stored = json.loads(im_settings.cipher().decrypt(row.encrypted.encode())) if row else {}
    except Exception:
        raise HTTPException(503, 'Personal OAuth configuration unavailable') from None
    prefix = 'PLATFORM_' + provider.upper() + '_'
    result = {f: stored.get(f, os.getenv(prefix + f, '')) for f in ('CLIENT_ID', 'CLIENT_SECRET')}
    # Scopes are governed by code, never by stored copies, so a scope change applies everywhere at once.
    result['SCOPES'] = SCOPES[provider]
    if '_source' in stored:
        result['_source'] = stored['_source']
    return result, row.revision if row else 0


@contextmanager
def snapshot(db, provider):
    data, _ = effective(db, provider)
    token = _active.set(data)
    try:
        yield data
    finally:
        _active.reset(token)


def values(provider):
    return _active.get() or {f: os.getenv('PLATFORM_' + provider.upper() + '_' + f,
        SCOPES[provider] if f == 'SCOPES' else '') for f in ('CLIENT_ID', 'CLIENT_SECRET', 'SCOPES')}


def bot_candidate(db, provider):
    data, revision = im_settings.effective(db, provider)
    prefix = provider.upper() + '_'
    client_id = data[prefix + ('APP_ID' if provider == 'feishu' else 'CLIENT_ID')]
    secret = data[prefix + ('APP_SECRET' if provider == 'feishu' else 'CLIENT_SECRET')]
    supported = provider == 'feishu' or data[prefix + 'TRANSPORT'] == 'stream'
    return {'available': bool(supported and client_id and secret), 'revision': revision,
            'client_id': client_id, 'snapshot': im_settings.fingerprint(data),
            'message': ('机器人应用字段可复制；设备授权能力尚未验证。' if supported else
                        '钉钉旧 webhook 密钥不是 OAuth 应用凭据；请先配置 Stream 应用。')}


def readiness(db, provider):
    data, _ = effective(db, provider)
    if not data['CLIENT_ID'] or not data['CLIENT_SECRET']:
        return 'configuration_missing'
    source = data.get('_source')
    if source and source.get('fingerprint') != bot_candidate(db, provider)['snapshot']:
        return 'configuration_changed'
    return None


def view(db, provider):
    data, revision = effective(db, provider)
    candidate = bot_candidate(db, provider)
    source = data.get('_source', {})
    return {'provider': provider, 'revision': revision, 'source': 'database' if revision else 'environment',
            'fields': {'CLIENT_ID': data['CLIENT_ID'], 'SCOPES': data['SCOPES']},
            'secrets_set': {'CLIENT_SECRET': bool(data['CLIENT_SECRET'])},
            'configured': bool(data['CLIENT_ID'] and data['CLIENT_SECRET']),
            'credential_source': source.get('mode', 'manual'), 'bot_revision': source.get('revision'),
            'bot_copy_stale': bool(source and source.get('fingerprint') != candidate['snapshot']),
            'bot_app': candidate,
            'message': '设备授权仅向官方服务发出 HTTPS 请求，无需 Hub 公网或回调。应用须已开通用户权限和设备授权能力；同一应用不保证平台允许授权。钉钉还需组织对本人开通 CLI 数据访问。'}


class Update(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    fields: dict[str, str] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


class UseBotApp(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    bot_revision: int = Field(ge=0)
    bot_snapshot: str = Field(pattern=r'^[a-f0-9]{16}$')
    confirm: bool


def persist(db, provider, data, revision):
    encrypted = im_settings.cipher().encrypt(json.dumps(data).encode()).decode()
    row = db.get(PlatformSettings, provider)
    if row:
        row.encrypted, row.revision = encrypted, revision + 1
    else:
        db.add(PlatformSettings(provider=provider, encrypted=encrypted, revision=1))
    db.flush()
    return view(db, provider)


def use_bot_app(db, provider, body):
    im_settings.provider_check(provider)
    if body.confirm is not True:
        raise HTTPException(400, '请管理员明确确认复制当前机器人应用')
    # Freeze both revisions, in the same order as independent OAuth writes.
    db.execute(select(func.pg_advisory_xact_lock(71911 if provider == 'feishu' else 71912)))
    db.execute(select(func.pg_advisory_xact_lock(71901 if provider == 'feishu' else 71902)))
    db.expire_all()
    _, revision = effective(db, provider)
    candidate = bot_candidate(db, provider)
    if (revision != body.revision or candidate['revision'] != body.bot_revision
            or candidate['snapshot'] != body.bot_snapshot):
        raise HTTPException(409, '配置已变化，请刷新后重新确认')
    if not candidate['available']:
        raise HTTPException(400, '机器人应用凭据不完整或旧 webhook 不支持复用；请配置完整应用')
    data, _ = im_settings.effective(db, provider)
    prefix = provider.upper() + '_'
    secret = data[prefix + ('APP_SECRET' if provider == 'feishu' else 'CLIENT_SECRET')]
    if any(set(v.strip()) <= {'*', '•', '●'} for v in (candidate['client_id'], secret)):
        raise HTTPException(400, '不能复制掩码占位值')
    return persist(db, provider, {'CLIENT_ID': candidate['client_id'], 'CLIENT_SECRET': secret,
        'SCOPES': SCOPES[provider], '_source': {'mode': 'bot_app_copy', 'revision': candidate['revision'],
                                            'fingerprint': candidate['snapshot']}}, revision)


def use_bot_app_now(db, provider):
    # Admin-triggered from IM: never trust model-supplied revision/snapshot values,
    # always re-read the live state server-side before delegating to use_bot_app().
    _, revision = effective(db, provider)
    candidate = bot_candidate(db, provider)
    return use_bot_app(db, provider, UseBotApp(revision=revision, bot_revision=candidate['revision'],
        bot_snapshot=candidate['snapshot'], confirm=True))


def save(db, provider, body):
    if provider not in SCOPES or set(body.fields) - {'CLIENT_ID', 'CLIENT_SECRET', 'SCOPES'} or set(body.clear) - {'CLIENT_ID', 'CLIENT_SECRET'}:
        raise HTTPException(400, 'Invalid personal OAuth configuration')
    for name, value in body.fields.items():
        if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value) or (value.strip() and set(value.strip()) <= {'*', '•', '●'}):
            raise HTTPException(400, 'Invalid personal OAuth field')
        if name in body.clear and value.strip():
            raise HTTPException(400, 'Cannot replace and clear the same field')
        if name == 'SCOPES' and value.strip() and set(value.split()) != set(SCOPES[provider].split()):
            raise HTTPException(400, 'Only the approved minimum personal scopes are allowed')
    db.execute(select(func.pg_advisory_xact_lock(71911 if provider == 'feishu' else 71912)))
    db.expire_all()
    data, revision = effective(db, provider)
    if revision != body.revision:
        raise HTTPException(409, '配置已变化，请刷新后重新保存')
    for name, value in body.fields.items():
        if value.strip():
            data[name] = value.strip()
    for name in body.clear:
        data[name] = ''
    if body.clear or any(body.fields.get(f, '').strip() for f in ('CLIENT_ID', 'CLIENT_SECRET')):
        data.pop('_source', None)
    return persist(db, provider, data, revision)
