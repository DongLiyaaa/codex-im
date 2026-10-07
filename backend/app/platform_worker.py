"""Leased PostgreSQL authorization worker; sends/exchanges are never replayed after ambiguity."""
import threading
from datetime import timedelta
from sqlalchemy import select, or_
from .db import SessionLocal
from .models import Identity, PlatformAuthJob, PlatformConnection, User, now
from . import platform_auth as pa, platform_broker as broker, platform_settings as settings

# Consecutive transient poll failures tolerated before an authorization is given up (about a minute at 5s).
MAX_POLL_FAILURES = 12


def identity_matches(provider, tokens, recipient, values, corp=None):
    """Whether the authorized account is the bound person; ProviderError when the platform cannot tell."""
    token = tokens.get('access_token')
    if not isinstance(token, str) or not token or len(token) > 8192:
        return False
    if provider == 'feishu':
        data = pa.request('GET', 'https://open.feishu.cn/open-apis/authen/v1/user_info', headers={'Authorization': 'Bearer ' + token})
        return data.get('code') == 0 and (data.get('data') or {}).get('open_id') == recipient
    # A DingTalk staff id only means this person inside the robot's organization, which is known once they have
    # messaged the robot from inside it.
    if not corp:
        raise pa.ProviderError('identity_unverified', 'IDENTITY_ORG_UNKNOWN')
    if tokens.get('user_id'):
        # Named by the official CLI exchange.
        return tokens.get('corp_id') == corp and tokens['user_id'] == recipient
    # Otherwise ask the contact MCP server whose token this is, as dws does.
    accounts = pa.dingtalk_accounts(token)
    if any(org == corp and user == recipient for org, user in accounts):
        return True
    # An account without its organization only names the person when it is the only one (dws accepts it likewise).
    return (len(accounts) == 1 and accounts[0] == (None, recipient)
            and tokens.get('corp_id') in (None, corp))


def corp_of(factory, provider, recipient):
    if provider != 'dingtalk' or not recipient:
        return None
    with factory() as db:
        return db.scalar(select(Identity.corp_id).where(Identity.provider == provider, Identity.external_user_id == recipient))


def verified(factory, provider, tokens, recipient, values):
    try:
        return bool(recipient) and identity_matches(provider, tokens, recipient, values, corp_of(factory, provider, recipient))
    except pa.ProviderError as exc:
        # Authorization itself succeeded; only "is this the bound person" could not be answered.
        raise pa.ProviderError('identity_unverified', exc.reason or 'IDENTITY_LOOKUP_FAILED') from None
    except (ValueError, OSError):
        raise pa.ProviderError('identity_unverified', 'IDENTITY_LOOKUP_FAILED') from None


def tick(factory=SessionLocal):
    with factory.begin() as db:
        jobs = list(db.scalars(select(PlatformAuthJob).where(PlatformAuthJob.phase != 'done').limit(100)))
        identifiers = [(j.user_id, j.provider) for j in jobs]
    for user_id, provider in identifiers:
        perform(factory, user_id, provider)


def perform(factory, user_id, provider):
    with factory.begin() as db:
        with settings.snapshot(db, provider):
            row = pa.locked(db, user_id, provider)
            job = db.get(PlatformAuthJob, (user_id, provider))
            if not job or job.phase == 'done':
                return
            if job.lease_until:
                if job.lease_until > now():
                    return
                # Poll may have consumed an authCode; never repeat a claimed exchange.
                row.state, row.encrypted = 'interrupted', ''
                job.phase, job.error = 'done', 'AUTHORIZATION_INTERRUPTED'
                if job.delivery == 'sending':
                    job.delivery = 'ambiguous'
                if job.notification == 'sending':
                    job.notification = 'ambiguous'
                return
            if job.fingerprint != broker.fingerprint(provider) or row.state not in ('pending', 'connected', 'starting'):
                job.phase, job.notification = 'done', 'cancelled'
                if row.state in ('pending', 'connected'):
                    row.state, row.encrypted = 'expired', ''
                return
            if settings.readiness(db, provider):
                row.state, row.encrypted, job.phase, job.notification = settings.readiness(db, provider), '', 'done', 'cancelled'
                job.error = row.state.upper()
                return
            user = db.get(User, user_id)
            if not user or not user.active:
                row.state, row.encrypted, job.phase = 'expired', '', 'done'
                return
            operation = job.phase
            if operation == 'poll' and row.next_poll_at and row.next_poll_at > now():
                return
            recipient, im_values = None, None
            if job.identity_id or job.source_run_id:
                try:
                    recipient, im_values = broker.authorized_target(db, job, require_target=bool(job.identity_id))
                except Exception:
                    row.state, row.encrypted, job.phase, job.notification = 'expired', '', 'done', 'cancelled'
                    job.error = 'IDENTITY_OR_SCOPE_REVOKED'
                    return
            device = pa.unseal(row)
            if operation == 'deliver':
                if not recipient or not pa.valid_link(provider, device.get('url')):
                    row.state, row.encrypted, job.delivery, job.phase, job.error = 'private_delivery_failed', '', 'failed', 'done', 'PRIVATE_DELIVERY_UNAVAILABLE'
                    return
                job.delivery = 'sending'
            if operation == 'notify':
                if not recipient or job.notification != 'pending':
                    job.phase = 'done'
                    return
                job.notification = 'sending'
            job.lease_until = now() + timedelta(seconds=180)
            generation = job.generation
            config, _ = settings.effective(db, provider)
            from .platform_settings import _active
    # Both the durable lease and config snapshot have committed before remote I/O.
    active = _active.set(config)
    outcome, tokens, error, spent = None, {}, None, False
    try:
        if operation == 'begin':
            tokens = pa.begin(provider)
            outcome = 'pending'
        elif operation == 'deliver':
            broker.dispatch(provider, recipient, im_values,
                '请本人完成官方授权，勿转发。\n' + device['url'] + '\n设备码：' + device['user_code'] + '\n短时有效；完成后后台确认，请重新发送任务。', generation)
            outcome = 'delivered'
        elif operation == 'poll':
            outcome, tokens = pa.poll(provider, device)
            if outcome == 'connected':
                spent = True  # The one-time device code is consumed; polling again can never succeed.
                if not recipient:
                    # Web requests also require a current, scoped personal IM identity.
                    with factory.begin() as db:
                        identity, _, _ = broker.target(db, db.get(User, user_id), provider, require_delivery=False)
                        if identity:
                            recipient = identity.external_user_id
                            from . import im_settings
                            im_values, _ = im_settings.effective(db, provider)
                if (type(tokens.get('expires_in')) is not int or tokens['expires_in'] <= 0):
                    raise ValueError('invalid_token_expiry')
                if not verified(factory, provider, tokens, recipient, im_values):
                    outcome, tokens, error = 'identity_mismatch', {}, 'PLATFORM_IDENTITY_MISMATCH'
        elif operation == 'validate':
            if not recipient:
                with factory.begin() as db:
                    identity, _, _ = broker.target(db, db.get(User, user_id), provider, require_delivery=False)
                    recipient = identity.external_user_id if identity else None
                    from . import im_settings
                    im_values, _ = im_settings.effective(db, provider)
            outcome = 'valid' if verified(factory, provider, device, recipient, im_values) else 'expired'
        elif operation == 'notify':
            broker.dispatch(provider, recipient, im_values, '本人平台授权已完成。请重新发送任务；此前任务不会自动执行。', generation + '-complete')
            outcome = 'notified'
    except pa.ProviderError as exc:
        outcome, error = exc.state, exc.reason or exc.state.upper()
    except Exception:
        outcome = 'failed'
        error = ('PRIVATE_DELIVERY_FAILED' if operation == 'deliver' else 'PRIVATE_NOTIFICATION_FAILED' if operation == 'notify'
                 else 'PLATFORM_DEVICE_REJECTED' if operation == 'begin' else 'PLATFORM_POLL_FAILED')
    finally:
        _active.reset(active)
    with factory.begin() as db:
        with settings.snapshot(db, provider):
            row = pa.locked(db, user_id, provider)
            job = db.get(PlatformAuthJob, (user_id, provider))
            if not job or job.generation != generation or not job.lease_until:
                return
            job.lease_until = None
            if job.fingerprint != broker.fingerprint(provider) or row.state not in ('pending', 'connected', 'starting'):
                job.phase, job.notification = 'done', 'cancelled'
                return
            if settings.readiness(db, provider):
                row.state, row.encrypted, job.phase, job.notification = settings.readiness(db, provider), '', 'done', 'cancelled'
                job.error = row.state.upper()
                return
            if job.identity_id or job.source_run_id:
                try:
                    broker.authorized_target(db, job, require_target=bool(job.identity_id))
                except Exception:
                    row.state, row.encrypted, job.phase = 'expired', '', 'done'
                    return
            if operation == 'begin':
                if outcome == 'pending':
                    row.state, row.encrypted = 'pending', pa.seal(tokens)
                    row.expires_at = now() + timedelta(seconds=tokens['expires_in'])
                    row.next_poll_at = now() + timedelta(seconds=tokens['interval'])
                    job.phase = 'deliver' if job.delivery == 'queued' else 'poll'
                else:
                    row.state, row.encrypted, job.phase, job.error = outcome if outcome != 'failed' else 'platform_rejected', '', 'done', error
                    job.delivery, job.notification = 'not_requested', 'cancelled'
            elif operation == 'deliver':
                job.delivery, job.error = 'delivered' if outcome == 'delivered' else 'failed', error
                job.phase = 'poll' if outcome == 'delivered' else 'done'
                if outcome != 'delivered':
                    row.state, row.encrypted, job.notification = 'private_delivery_failed', '', 'cancelled'
            elif operation == 'validate':
                job.phase = 'done'
                if outcome != 'valid':
                    row.state, row.encrypted = 'expired', ''
            elif operation == 'notify':
                job.notification, job.phase, job.error = outcome, 'done', error
            elif outcome in ('authorization_pending', 'slow_down'):
                if outcome == 'slow_down' or device.get('failures'):
                    device['interval'] += 5 if outcome == 'slow_down' else 0
                    device['failures'] = 0
                    row.encrypted = pa.seal(device)
                row.next_poll_at = now() + timedelta(seconds=device['interval'])
            elif outcome == 'failed' and provider == 'feishu' and not spent and device.get('failures', 0) < MAX_POLL_FAILURES:
                # A network hiccup or 5xx must not end an authorization the user may be completing right now.
                # Feishu polls are safe to repeat until a token is issued; DingTalk's one-time auth code must never be re-claimed.
                device['failures'] = device.get('failures', 0) + 1
                row.encrypted = pa.seal(device)
                row.next_poll_at = now() + timedelta(seconds=device['interval'])
            elif outcome == 'connected':
                row.state, row.encrypted = 'connected', pa.seal(tokens)
                row.expires_at = now() + timedelta(seconds=max(1, min(int(tokens.get('expires_in', 7200)), 7200)))
                row.next_poll_at = None
                job.phase = 'notify' if job.delivery == 'delivered' else 'done'
            else:
                row.state, row.encrypted, job.phase, job.error = outcome if outcome != 'failed' else 'platform_rejected', '', 'done', error


class AuthWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name='pg-authorization-worker', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=90)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                tick()
            except Exception:
                # Upstream URLs, device codes and response bodies must never be logged.
                pass
            self.stop_event.wait(1)
