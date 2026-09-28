"""Independent, encrypted, revisioned personal OAuth configuration."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from .models import PlatformSettings
from . import im_settings

SCOPES = {'feishu': 'docx:document:readonly', 'dingtalk': 'openid corpid'}
_active = ContextVar('platform_settings', default=None)


def effective(db, provider):
    if provider not in SCOPES:
        raise HTTPException(404, 'Unknown platform')
    row = db.get(PlatformSettings, provider)
    try:
        import json
        values = json.loads(im_settings.cipher().decrypt(row.encrypted.encode())) if row else {}
    except Exception:
        raise HTTPException(503, 'Personal OAuth configuration unavailable') from None
    prefix = 'PLATFORM_' + provider.upper() + '_'
    result = {f: values.get(f, os.getenv(prefix + f, SCOPES[provider] if f == 'SCOPES' else ''))
              for f in ('CLIENT_ID', 'CLIENT_SECRET', 'SCOPES')}
    return result, row.revision if row else 0


@contextmanager
def snapshot(db, provider):
    values, _ = effective(db, provider)
    token = _active.set(values)
    try:
        yield values
    finally:
        _active.reset(token)


def values(provider):
    return _active.get() or {f: os.getenv('PLATFORM_' + provider.upper() + '_' + f,
        SCOPES[provider] if f == 'SCOPES' else '') for f in ('CLIENT_ID', 'CLIENT_SECRET', 'SCOPES')}


def view(db, provider):
    data, revision = effective(db, provider)
    return {'provider': provider, 'revision': revision, 'source': 'database' if revision else 'environment',
            'fields': {'CLIENT_ID': data['CLIENT_ID'], 'SCOPES': data['SCOPES']},
            'secrets_set': {'CLIENT_SECRET': bool(data['CLIENT_SECRET'])},
            'configured': bool(data['CLIENT_ID'] and data['CLIENT_SECRET']),
            'message': '独立于机器人配置；须在平台后台开通个人 OAuth / 设备授权。钉钉还需组织开通本人 CLI 数据访问。'}


class Update(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    fields: dict[str, str] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


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
        raise HTTPException(409, 'Configuration changed; reload before saving')
    for name, value in body.fields.items():
        if value.strip():
            data[name] = value.strip()
    for name in body.clear:
        data[name] = ''
    import json
    encrypted = im_settings.cipher().encrypt(json.dumps(data).encode()).decode()
    row = db.get(PlatformSettings, provider)
    if row:
        row.encrypted, row.revision = encrypted, revision + 1
    else:
        db.add(PlatformSettings(provider=provider, encrypted=encrypted, revision=1))
    db.flush()
    return view(db, provider)
