import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import OrderedDict
from fastapi import Depends, HTTPException, Request
from .db import get_db
from .models import SessionToken, User, now


def session_secret():
    value = os.getenv('SESSION_SECRET', '')
    if len(value) < 32:
        raise RuntimeError('SESSION_SECRET must contain at least 32 characters')
    return value


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'{salt.hex()}:{digest.hex()}'


def verify_password(password, encoded):
    try:
        salt, digest = encoded.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hmac.new(session_secret().encode(), token.encode(), hashlib.sha256).hexdigest()


def current_user(request: Request, db=Depends(get_db)):
    token = request.cookies.get('hub_session', '')
    session = db.get(SessionToken, token_hash(token)) if token else None
    if not session or session.expires_at <= now():
        raise HTTPException(401, 'Authentication required')
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(401, 'Account unavailable')
    return user


_lock = threading.Lock()
_attempts = OrderedDict()


def rate_limit(key):
    moment = time.monotonic()
    with _lock:
        entries = [t for t in _attempts.pop(key, []) if moment - t < 60]
        if len(entries) >= 10:
            _attempts[key] = entries
            raise HTTPException(429, 'Too many login attempts', headers={'Retry-After': '60'})
        _attempts[key] = entries + [moment]
        while len(_attempts) > 10000:
            _attempts.popitem(last=False)
