import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import timedelta
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, text

from .db import SessionLocal, get_db
from .models import CodexConnection, CodexLoginJob, Run, User, now, uid
from .security import current_user

router = APIRouter(tags=['codex-auth'])
LIMIT = 100_000
VERIFY_URL = 'https://auth.openai.com/codex/device'
STATES = {'starting': '正在发起官方授权', 'pending': '等待你在官方页面确认', 'connected': '本人 ChatGPT 已连接',
          'disconnected': '尚未连接本人 ChatGPT', 'expired': '授权已过期，请重新发起',
          'interrupted': '授权过程已中断，请重新发起', 'failed': '授权失败，请检查设备登录权限后重试'}


def cipher():
    secret = os.getenv('SESSION_SECRET', '')
    if len(secret) < 32:
        raise HTTPException(503, 'Codex credential encryption unavailable')
    key = hashlib.sha256(b'agent-hub/codex-auth/v1\0' + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value):
    return cipher().encrypt(json.dumps(value, ensure_ascii=True).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode())) if value else {}


def valid_auth(raw):
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        raise ValueError('INVALID_AUTH')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('INVALID_AUTH')
    tokens = data.get('tokens')
    if (data.get('auth_mode') != 'chatgpt' or data.get('OPENAI_API_KEY') or not isinstance(tokens, dict)
            or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'refresh_token', 'id_token'))):
        raise ValueError('INVALID_AUTH')
    return raw


def locked(db, user_id):
    db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'), {'key': 'codex-auth:' + user_id})
    row = db.get(CodexConnection, user_id, populate_existing=True)
    if row is None:
        row = CodexConnection(user_id=user_id)
        db.add(row)
        db.flush()
    return row


def runner(method, path, body=None):
    token = os.getenv('RUNNER_TOKEN', '')
    if len(token) < 32:
        raise HTTPException(503, '执行器未配置')
    try:
        with httpx.Client(base_url=os.getenv('RUNNER_URL', 'http://127.0.0.1:18201').rstrip('/'),
                          timeout=15, trust_env=False, follow_redirects=False) as client:
            with client.stream(method, path, json=body, headers={'Authorization': 'Bearer ' + token}) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > LIMIT:
                        raise ValueError()
                if response.status_code >= 400:
                    raise HTTPException(response.status_code if response.status_code in (404, 409, 429) else 503,
                                        'Codex 授权服务不可用，请检查执行器能力或稍后重试')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, 'Codex 授权服务暂不可用') from None


def mode():
    value = runner('GET', '/codex-auth/mode')
    if value.get('mode') not in ('personal', 'api', 'shared', 'unavailable'):
        raise HTTPException(503, '执行器尚不支持个人 Codex 授权')
    return {k: value.get(k) for k in ('mode', 'error')}


def public(db, actor):
    row = db.get(CodexConnection, actor.id)
    state = row.state if row else 'disconnected'
    result = {'state': state, 'message': STATES.get(state, '请重新授权'), 'account': row.account if row else {}}
    if row and state in ('starting', 'pending'):
        job = db.get(CodexLoginJob, row.generation)
        if not job or job.expires_at <= now():
            return {'state': 'expired', 'message': STATES['expired'], 'account': {}}
        if job.delivery == 'web' and job.encrypted_device:
            device = unseal(job.encrypted_device)
            if device.get('url') == VERIFY_URL:
                result.update(url=device['url'], user_code=device.get('user_code'), expires_at=job.expires_at.isoformat())
    return result


def start(db, actor, provider=None):
    from . import service, platform_broker
    row = locked(db, actor.id)
    if not db.get(User, actor.id, populate_existing=True).active:
        raise HTTPException(403, '用户已停用')
    if row.lease_until and row.lease_until > now():
        raise HTTPException(409, '本人 Codex 任务正在执行，请结束后再授权')
    existing = db.get(CodexLoginJob, row.generation)
    if (existing and existing.phase not in ('cleanup', 'done') and existing.expires_at > now()
            and (provider is None or existing.provider == provider)):
        return public(db, actor)
    identity = scope = fingerprint = None
    if provider:
        identity, scope, fingerprint = platform_broker.target(db, actor, provider)
        if not identity:
            raise HTTPException(409, '当前渠道不能安全私发授权，请检查本人绑定及机器人私聊支持')
    invalidate(db, actor.id)
    row.generation, row.state = uid(), 'starting'
    job = CodexLoginJob(id=row.generation, user_id=actor.id, expires_at=now() + timedelta(minutes=10),
                        provider=provider, identity_id=identity.id if identity else None, app_scope=scope,
                        im_fingerprint=fingerprint, delivery='queued' if provider else 'web')
    db.add(job)
    service.audit(db, actor, 'codex.authorization_start', actor.id)
    return {'state': 'starting', 'message': STATES['starting'], 'account': {}}


def invalidate(db, user_id):
    row = locked(db, user_id)
    for job in db.scalars(select(CodexLoginJob).where(CodexLoginJob.user_id == user_id, CodexLoginJob.phase != 'done')):
        job.phase, job.encrypted_device = 'cleanup', ''
    row.generation, row.state, row.encrypted, row.account = uid(), 'disconnected', '', {}
    row.lease_run_id = row.ticket_hash = row.lease_until = None
    row.claimed = row.saved = False


@router.get('/api/auth/codex')
def status(actor=Depends(current_user), db=Depends(get_db, scope='function')):
    return {**public(db, actor), **mode()}


@router.post('/api/auth/codex/start')
def begin(actor=Depends(current_user), db=Depends(get_db, scope='function')):
    available = mode()
    if available['mode'] != 'personal':
        raise HTTPException(409, '当前使用系统凭据或配置不可用，无需发起个人授权')
    return start(db, actor)


@router.post('/api/auth/codex/disconnect')
def disconnect(actor=Depends(current_user), db=Depends(get_db, scope='function')):
    from .service import audit
    invalidate(db, actor.id)
    audit(db, actor, 'codex.authorization_disconnect', actor.id)
    return {'state': 'disconnected', 'message': STATES['disconnected'], 'account': {}}


def reserve(db, run):
    row = locked(db, run.user_id)
    if run.status != 'running' or not db.get(User, run.user_id, populate_existing=True).active:
        raise RuntimeError('任务已取消或用户已停用')
    if row.state != 'connected' or not row.encrypted:
        raise RuntimeError('请先登录本人 ChatGPT；网页点击授权，IM 私聊发送 /codex-login')
    if row.lease_until:
        if row.lease_until > now():
            raise RuntimeError('本人 Codex 任务正在执行，请稍后重试')
        if row.claimed and not row.saved:
            row.state, row.encrypted = 'expired', ''
            raise RuntimeError('本人 ChatGPT 授权需重新连接')
    ticket = secrets.token_urlsafe(32)
    row.lease_run_id, row.lease_until, row.ticket_hash = run.id, now() + timedelta(seconds=240), hashlib.sha256(ticket.encode()).hexdigest()
    row.claimed = row.saved = False
    return ticket


def internal(request):
    expected = os.getenv('RUNNER_TOKEN', '')
    if len(expected) < 32 or not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + expected):
        raise HTTPException(401, 'UNAUTHORIZED')


async def payload(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > LIMIT:
            raise HTTPException(413, 'REQUEST_TOO_LARGE')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except ValueError:
        raise HTTPException(422, 'INVALID_REQUEST') from None


@router.post('/internal/codex-auth/{run_id}/{operation}')
async def credential_exchange(run_id: str, operation: str, request: Request, db=Depends(get_db, scope='function')):
    internal(request)
    body = await payload(request)
    run = db.get(Run, run_id)
    if not run or run.agent != 'codex':
        raise HTTPException(404, 'NOT_FOUND')
    row = locked(db, run.user_id)
    ticket = body.get('ticket')
    if (not isinstance(ticket, str) or not row.ticket_hash or not hmac.compare_digest(row.ticket_hash, hashlib.sha256(ticket.encode()).hexdigest())
            or row.lease_run_id != run.id or not row.lease_until or row.lease_until <= now()
            or not db.get(User, run.user_id).active or row.state != 'connected'):
        raise HTTPException(403, 'CREDENTIAL_LEASE_REVOKED')
    if operation == 'claim':
        from .service import build_payload
        build_payload(db, run)
        if run.status != 'running' or row.claimed:
            raise HTTPException(409, 'CREDENTIAL_ALREADY_CLAIMED')
        row.claimed = True
        return {'auth': valid_auth(unseal(row.encrypted)['auth'])}
    if operation == 'save':
        if not row.claimed:
            raise HTTPException(409, 'CREDENTIAL_NOT_CLAIMED')
        try:
            raw = valid_auth(body.get('auth'))
        except (ValueError, TypeError):
            raise HTTPException(422, 'INVALID_AUTH') from None
        if row.saved and unseal(row.encrypted).get('auth') != raw:
            raise HTTPException(409, 'CREDENTIAL_ALREADY_SAVED')
        row.encrypted = seal({'auth': raw})
        row.saved = True
        return {'ok': True}
    raise HTTPException(404, 'NOT_FOUND')


def finish(db, run, ticket):
    row = locked(db, run.user_id)
    if row.lease_run_id == run.id:
        if row.claimed and not row.saved:
            row.state, row.encrypted = 'expired', ''
        row.lease_run_id = row.ticket_hash = row.lease_until = None
        row.claimed = row.saved = False


def check_delivery(db, job):
    from . import platform_broker
    return platform_broker.authorized_target(db, SimpleNamespace(user_id=job.user_id, provider=job.provider,
        identity_id=job.identity_id, source_run_id=None, app_scope=job.app_scope, im_fingerprint=job.im_fingerprint))


def perform(identifier, factory=SessionLocal):
    with factory.begin() as db:
        original = db.get(CodexLoginJob, identifier)
        if not original:
            return
        row = locked(db, original.user_id)
        job = db.get(CodexLoginJob, identifier, populate_existing=True)
        if job.phase == 'done':
            return
        if job.lease_until and job.lease_until > now():
            return
        user = db.get(User, job.user_id)
        if job.phase != 'cleanup' and (job.lease_until or job.expires_at <= now() or not user.active or row.generation != job.id):
            if row.generation == job.id:
                row.state = 'expired' if job.expires_at <= now() else 'interrupted'
            job.phase, job.encrypted_device = 'cleanup', ''
        operation = job.phase
        job.lease_until = now() + timedelta(seconds=45)
        delivery = job.delivery
        device = unseal(job.encrypted_device)
        recipient = values = None
        if operation == 'deliver':
            try:
                recipient, values = check_delivery(db, job)
                if not recipient or device.get('url') != VERIFY_URL:
                    raise ValueError()
                job.delivery = 'sending'
            except Exception:
                job.phase, job.lease_until, job.encrypted_device = 'cleanup', None, ''
                row.state = 'failed'
                return
    try:
        if operation == 'start':
            if mode()['mode'] != 'personal':
                raise ValueError()
            result = runner('POST', '/codex-auth/login/' + identifier)
        elif operation == 'poll':
            result = runner('GET', '/codex-auth/login/' + identifier + '/result')
        elif operation == 'deliver':
            from .platform_broker import dispatch
            dispatch(job.provider, recipient, values, '仅供本人：请打开官方链接登录 ChatGPT，勿转发。\n' + device['url']
                     + '\n一次性代码：' + device['user_code'] + '\n10 分钟内有效，完成后请重新发送任务。', identifier)
            result = {'state': 'pending'}
        else:
            try:
                runner('DELETE', '/codex-auth/login/' + identifier)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            result = {'state': 'done'}
        error = None
    except Exception:
        result, error = {}, 'CODEX_AUTH_SERVICE_FAILED'
    with factory.begin() as db:
        row = locked(db, job.user_id)
        current = db.get(CodexLoginJob, identifier, populate_existing=True)
        if current.phase != operation:
            current.lease_until = None
            return
        current.lease_until = None
        if operation == 'cleanup':
            current.phase = 'done' if not error else 'cleanup'
            return
        if row.generation != identifier or not db.get(User, job.user_id).active:
            current.phase, current.encrypted_device = 'cleanup', ''
            return
        state = result.get('state')
        if error or state in ('expired', 'failed', 'cancelled'):
            row.state, row.encrypted = 'failed', ''
            current.phase, current.encrypted_device, current.error = 'cleanup', '', error or 'CODEX_AUTH_FAILED'
        elif state == 'connected':
            try:
                raw = valid_auth(result.get('auth'))
                if current.provider:
                    check_delivery(db, current)
                account = result.get('account') or {}
                row.account = {k: v[:200] for k in ('email', 'planType') if isinstance((v := account.get(k)), str)}
                row.encrypted, row.state = seal({'auth': raw}), 'connected'
                current.phase, current.encrypted_device = 'cleanup', ''
            except Exception:
                row.state, current.phase, current.encrypted_device = 'failed', 'cleanup', ''
        elif operation == 'deliver':
            current.delivery, current.phase = 'delivered', 'poll'
        elif state == 'pending':
            if result.get('url') != VERIFY_URL or not isinstance(result.get('user_code'), str) or len(result['user_code']) > 32:
                row.state, current.phase = 'failed', 'cleanup'
            else:
                current.encrypted_device = seal({'url': result['url'], 'user_code': result['user_code']})
                row.state = 'pending'
                current.phase = 'deliver' if delivery == 'queued' else 'poll'
        else:
            current.phase = 'poll'


class AuthWorker:
    def __init__(self):
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=20)

    def loop(self):
        while not self.stopping.wait(3):
            try:
                with SessionLocal() as db:
                    ids = list(db.scalars(select(CodexLoginJob.id).where(CodexLoginJob.phase != 'done').limit(100)))
                for identifier in ids:
                    if self.stopping.is_set():
                        break
                    perform(identifier)
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Codex authorization worker unavailable')
