"""MCP request-header values are secrets: they are stored encrypted and only the run payload ever reads them back.

The key is derived from SESSION_SECRET with its own domain label, so it is unrelated to the IM configuration key. A
stored value is `enc1:` plus a Fernet token. Values without the prefix are older plaintext and are passed through
unchanged, so resources created before this existed keep working.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

PREFIX = 'enc1:'


def cipher():
    secret = os.getenv('SESSION_SECRET', '')
    if len(secret) < 32:
        raise HTTPException(503, 'Resource secret key unavailable')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'agent-hub/resource-secrets/v1\0' + secret.encode()).digest()))


def headers_of(kind, config):
    headers = config.get('headers') if kind == 'mcp' and isinstance(config, dict) else None
    return headers if isinstance(headers, dict) else {}


def seal_config(kind, config):
    """A copy of an already validated config with every header value encrypted."""
    headers = headers_of(kind, config)
    if not headers:
        return config
    box = cipher()
    return {**config, 'headers': {name: PREFIX + box.encrypt(value.encode()).decode() for name, value in headers.items()}}


def reveal_config(kind, config):
    """A copy of a stored config with the header values decrypted (older plaintext values are kept as they are)."""
    headers = headers_of(kind, config)
    if not any(isinstance(value, str) and value.startswith(PREFIX) for value in headers.values()):
        return config
    box = cipher()
    revealed = {}
    for name, value in headers.items():
        if isinstance(value, str) and value.startswith(PREFIX):
            try:
                value = box.decrypt(value[len(PREFIX):].encode()).decode()
            except InvalidToken:
                # Typically SESSION_SECRET changed since the resource was saved; never fall back to sending the token text.
                raise HTTPException(503, 'MCP 请求头密钥无法解密，请确认 SESSION_SECRET 未变更，或重新创建该 MCP。') from None
        revealed[name] = value
    return {**config, 'headers': revealed}


def has_headers(kind, config):
    return bool(headers_of(kind, config))


MASK = '***'


def merge_config(kind, config, stored):
    """The config of an edited resource, sealed. A header whose value is still the display mask keeps its stored secret."""
    headers = headers_of(kind, config)
    if not headers:
        return config
    previous = headers_of(kind, stored)
    box = None
    sealed = {}
    for name, value in headers.items():
        if value == MASK and name in previous:
            sealed[name] = previous[name]
            continue
        box = box or cipher()
        sealed[name] = PREFIX + box.encrypt(value.encode()).decode()
    return {**config, 'headers': sealed}
