"""Platform IM credentials: authenticated encryption, atomic revisioned writes."""
import base64
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func

from .models import IMSettings

FIELDS = {
    'feishu': ['APP_ID', 'APP_SECRET', 'VERIFICATION_TOKEN', 'ENCRYPT_KEY'],
    'dingtalk': ['CLIENT_ID', 'CLIENT_SECRET', 'ROBOT_CODE', 'APP_SECRET', 'ROBOT_ACCESS_TOKEN', 'ROBOT_CHAT_ID', 'ROBOT_SECRET'],
}
PUBLIC = {'APP_ID', 'CLIENT_ID', 'ROBOT_CODE', 'ROBOT_CHAT_ID'}
_active = ContextVar('im_settings', default=None)


def cipher():
    key = os.getenv('IM_CONFIG_KEY')
    try:
        if key:
            return Fernet(key.encode())
        secret = os.getenv('SESSION_SECRET', '')
        if len(secret) < 32:
            raise ValueError()
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'agent-hub/im-config/v1\0' + secret.encode()).digest()))
    except ValueError:
        raise HTTPException(503, 'IM configuration key unavailable') from None


def provider_check(provider):
    if provider not in FIELDS:
        raise HTTPException(404, 'Not found')


def read(db, provider):
    provider_check(provider)
    row = db.get(IMSettings, provider)
    if row:
        try:
            values = json.loads(cipher().decrypt(row.encrypted.encode()))
        except (InvalidToken, ValueError):
            raise HTTPException(503, 'IM configuration cannot be decrypted') from None
        return values, row.revision
    return {}, 0


def effective(db, provider):
    values, revision = read(db, provider)
    prefix = provider.upper() + '_'
    result = {prefix + field: values.get(field, os.getenv(prefix + field, '')) for field in FIELDS[provider] + ['TRANSPORT']}
    result[prefix + 'TRANSPORT'] = result[prefix + 'TRANSPORT'] or 'webhook'
    return result, revision


@contextmanager
def snapshot(db, provider):
    values, _ = effective(db, provider)
    token = _active.set(values)
    try:
        yield values
    finally:
        _active.reset(token)


def value(name, default=''):
    values = _active.get()
    return values.get(name, default) if values is not None else os.getenv(name, default)


def fingerprint(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


def view(db, provider):
    values, revision = effective(db, provider)
    prefix = provider.upper() + '_'
    return {'provider': provider, 'revision': revision, 'source': 'database' if revision else 'environment',
            'transport': values[prefix + 'TRANSPORT'],
            'fields': {field: values[prefix + field] for field in FIELDS[provider] if field in PUBLIC},
            'secrets_set': {field: bool(values[prefix + field]) for field in FIELDS[provider] if field not in PUBLIC}}


class Update(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    transport: str = Field(max_length=20)
    fields: dict[str, str] = Field(default_factory=dict)
    clear: list[str] = Field(default_factory=list)


def save(db, provider, body):
    provider_check(provider)
    if body.transport not in ('webhook', 'websocket' if provider == 'feishu' else 'stream'):
        raise HTTPException(400, 'Invalid IM transport')
    if set(body.fields) - set(FIELDS[provider]) or set(body.clear) - set(FIELDS[provider]):
        raise HTTPException(400, 'Unknown IM field')
    for field, text in body.fields.items():
        if len(text) > 4096 or (text.strip() and set(text.strip()) <= {'*', '•', '●'}):
            raise HTTPException(400, 'Invalid IM field value')
        if field in body.clear and text.strip():
            raise HTTPException(400, 'Cannot replace and clear the same field')
    # Bound advisory lock also serializes the first INSERT (no existing row to lock).
    db.execute(select(func.pg_advisory_xact_lock(71901 if provider == 'feishu' else 71902)))
    db.expire_all()
    values, revision = effective(db, provider)
    if revision != body.revision:
        raise HTTPException(409, 'Configuration changed; reload before saving')
    prefix = provider.upper() + '_'
    updated = {field: values[prefix + field] for field in FIELDS[provider]}
    for field, text in body.fields.items():
        if text.strip():
            updated[field] = text.strip()
    for field in body.clear:
        updated[field] = ''  # Tombstone: never fall back to an environment secret.
    updated['TRANSPORT'] = body.transport
    encrypted = cipher().encrypt(json.dumps(updated).encode()).decode()
    row = db.get(IMSettings, provider)
    from .models import now, IMConnection
    if row is None:
        row = IMSettings(provider=provider, encrypted=encrypted, revision=1)
        db.add(row)
    else:
        row.encrypted, row.revision, row.updated_at = encrypted, revision + 1, now()
    connection = db.get(IMConnection, provider)
    if connection:
        connection.state = 'configuration_changed'
    db.flush()
    return view(db, provider)
