"""Personal authorization broker. No private material enters messages or tool output."""
import hashlib
from datetime import timedelta
from sqlalchemy import select
from .models import (PlatformAuthJob, PlatformAuthRequest, PlatformConnection, Identity,
                     IMEvent, Run, Message, User, Conversation, now, uid)
from . import platform_auth as pa, platform_settings as settings, im_settings, im_discovery, policy


def fingerprint(provider):
    return hashlib.sha256(('\0'.join(pa.credentials(provider)) + '\0' + pa.SCOPES[provider]).encode()).hexdigest()


def target(db, actor, provider, require_delivery=True):
    with im_settings.snapshot(db, provider) as values:
        scope = im_discovery.scope(provider)
        candidates = list(db.scalars(select(Identity).where(Identity.user_id == actor.id, Identity.provider == provider)))
        identities = [i for i in candidates if im_discovery.pinned(db, 'identity', i.id, scope)]
        if len(identities) != 1:
            return None, scope, im_settings.fingerprint(values)
        if require_delivery and provider == 'dingtalk' and im_settings.value('DINGTALK_TRANSPORT', 'webhook') != 'stream':
            return None, scope, im_settings.fingerprint(values)
        return identities[0], scope, im_settings.fingerprint(values)


def status(db, actor, provider, private=False):
    with settings.snapshot(db, provider):
        row = pa.locked(db, actor.id, provider)
        job = db.get(PlatformAuthJob, (actor.id, provider))
        if job and job.fingerprint != fingerprint(provider) and row.state in ('pending', 'connected', 'starting'):
            row.state, row.encrypted = 'expired', ''
            job.phase, job.notification = 'done', 'cancelled'
        if job and job.identity_id and row.state in ('pending', 'connected', 'starting'):
            try:
                authorized_target(db, job)
            except Exception:
                row.state, row.encrypted = 'expired', ''
                job.phase, job.notification, job.error = 'done', 'cancelled', 'IDENTITY_OR_SCOPE_REVOKED'
        if job and job.phase != 'done' and job.source_run_id:
            try:
                authorized_target(db, job, require_target=bool(job.identity_id))
            except Exception:
                row.state, row.encrypted = 'expired', ''
                job.phase, job.notification, job.error = 'done', 'cancelled', 'IDENTITY_OR_SCOPE_REVOKED'
        if row.state == 'expired' and job:
            job.phase, job.notification = 'done', 'cancelled'
        problem = settings.readiness(db, provider)
        if problem:
            row.state, row.encrypted = problem, ''
            if job:
                job.phase, job.notification, job.delivery, job.error = 'done', 'cancelled', 'not_requested', problem.upper()
        result = pa.view(row, private)
        if private and row.state == 'pending' and result.get('authorization_url') and not pa.valid_link(provider, result['authorization_url']):
            result.pop('authorization_url', None)
            result.pop('user_code', None)
        result['delivery_status'] = job.delivery if job else 'not_requested'
        result['error_code'] = job.error if job else problem.upper() if problem else row.state.upper() if row.state in pa.MESSAGES and row.state not in ('disconnected', 'pending', 'starting', 'connected') else None
        return result


def marker(db, actor, run, provider):
    existing = db.scalar(select(PlatformAuthRequest).where(PlatformAuthRequest.run_id == run.id, PlatformAuthRequest.provider == provider))
    if existing:
        return
    message = Message(conversation_id=run.conversation_id, role='system', content='个人平台授权请求')
    db.add(message); db.flush()
    db.add(PlatformAuthRequest(run_id=run.id, user_id=actor.id, provider=provider, message_id=message.id))
    db.flush()


def markers(db, actor, messages):
    identifiers = [m['id'] for m in messages]
    requests = list(db.scalars(select(PlatformAuthRequest).where(PlatformAuthRequest.message_id.in_(identifiers))))
    lookup = {r.message_id: r for r in requests}
    for message in messages:
        request = lookup.get(message['id'])
        if request:
            # Even supervisors and other group members get only a non-interactive marker.
            row = db.get(PlatformConnection, (request.user_id, request.provider))
            message['platform_authorization'] = {'provider': request.provider,
                'state': row.state if row else 'disconnected', 'can_open': request.user_id == actor.id}
    return messages


def start(db, actor, provider, run=None, private=False):
    with settings.snapshot(db, provider):
        if run and (run.user_id != actor.id or run.status != 'running'):
            from fastapi import HTTPException
            raise HTTPException(403, 'Authorization request is not active')
        if run:
            from .service import build_payload
            build_payload(db, run)
        row = pa.locked(db, actor.id, provider)
        if run and db.scalar(select(PlatformAuthRequest.id).where(PlatformAuthRequest.run_id == run.id, PlatformAuthRequest.provider == provider)):
            return status(db, actor, provider, private)
        if run:
            marker(db, actor, run, provider)
        if row.state in ('pending', 'connected', 'starting'):
            result = status(db, actor, provider, private)
            if result['state'] in ('pending', 'connected', 'starting'):
                return result
        problem = settings.readiness(db, provider)
        if problem:
            row.state, row.encrypted = problem, ''
            return status(db, actor, provider, private)
        from .security import rate_limit
        rate_limit('platform-start:' + actor.id + ':' + provider)
        identity, scope, im_fp = None, None, None
        delivery = 'web'
        im_source = run and db.scalar(select(IMEvent.id).where(IMEvent.run_id == run.id))
        try:
            identity, scope, im_fp = target(db, actor, provider, require_delivery=False)
        except Exception:
            pass
        problem = None
        if not identity:
            problem = 'identity_missing'
        with im_settings.snapshot(db, provider) as im_values:
            bot_id = im_values.get('FEISHU_APP_ID' if provider == 'feishu' else 'DINGTALK_CLIENT_ID')
            if identity and bot_id != pa.credentials(provider)[0]:
                problem = 'identity_app_mismatch'
            elif identity and im_source and provider == 'dingtalk' and im_values.get('DINGTALK_TRANSPORT') != 'stream':
                # Only a resolved target's transport support can override; a missing
                # identity must keep reporting identity_missing, not this unrelated check.
                problem = 'private_delivery_unsupported'
        delivery = 'queued' if im_source and not problem else 'web'
        if problem:
            row.state, row.encrypted = problem, ''
            old_job = db.get(PlatformAuthJob, (actor.id, provider))
            if old_job:
                old_job.phase, old_job.notification, old_job.delivery, old_job.error = 'done', 'cancelled', 'not_requested', problem.upper()
            return status(db, actor, provider, private)
        job = db.get(PlatformAuthJob, (actor.id, provider))
        if not job:
            job = PlatformAuthJob(user_id=actor.id, provider=provider, fingerprint=fingerprint(provider))
            db.add(job)
        job.generation, job.phase, job.lease_until = uid(), 'begin', None
        job.fingerprint, job.identity_id = fingerprint(provider), identity.id if identity else None
        job.source_run_id = run.id if run else None
        job.im_fingerprint, job.app_scope = im_fp, scope
        job.delivery, job.notification, job.error = delivery, 'pending', None
        row.state, row.encrypted = 'starting', ''
        db.flush()
        return status(db, actor, provider, private)


def operate(db, actor, provider, action='status', private=False, run=None):
    if action == 'start':
        return start(db, actor, provider, run, private)
    if action in ('cancel', 'disconnect'):
        with settings.snapshot(db, provider):
            row = pa.locked(db, actor.id, provider)
            row.state, row.encrypted, row.expires_at, row.next_poll_at = 'disconnected', '', None, None
            job = db.get(PlatformAuthJob, (actor.id, provider))
            if job:
                job.generation, job.phase, job.notification = uid(), 'done', 'cancelled'
            from .service import audit
            audit(db, actor, 'platform.' + action, provider, {'state': row.state})
    # Pending refresh is read-only: only the leased worker exchanges tokens.
    elif action == 'refresh':
        with settings.snapshot(db, provider):
            row = pa.locked(db, actor.id, provider)
            if row.state == 'connected':
                job = db.get(PlatformAuthJob, (actor.id, provider))
                if job and job.phase == 'done':
                    job.phase = 'validate'
    return status(db, actor, provider, private)


def authorized_target(db, job, require_target=True):
    user = db.get(User, job.user_id)
    if not user or not user.active:
        raise ValueError('inactive')
    if job.source_run_id:
        run = db.get(Run, job.source_run_id)
        conversation = db.get(Conversation, run.conversation_id) if run else None
        if not run or run.user_id != job.user_id or run.status in ('cancelled', 'interrupted') or not conversation or not policy.can_send_conversation(db, user, conversation):
            raise ValueError('revoked')
        event = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id))
        if event:
            with im_settings.snapshot(db, event.provider):
                scope = im_discovery.scope(event.provider)
                t = event.reply_target
                rejection, mapped, group = im_discovery.reason(db, event.provider, scope, t['sender_id'], t['chat_id'], bool(t.get('group_id')))
                if (t.get('app_scope') != scope or rejection or mapped.id != user.id
                        or t.get('user_id') != user.id or t.get('conversation_id') != conversation.id
                        or t.get('group_id') != conversation.group_id):
                    raise ValueError('source_changed')
    if not require_target:
        return None, None
    identity = db.get(Identity, job.identity_id) if job.identity_id else None
    with im_settings.snapshot(db, job.provider) as values:
        if (not identity or identity.user_id != job.user_id or identity.provider != job.provider
                or im_discovery.scope(job.provider) != job.app_scope
                or im_settings.fingerprint(values) != job.im_fingerprint
                or not im_discovery.pinned(db, 'identity', identity.id, job.app_scope)):
            raise ValueError('identity_changed')
        return identity.external_user_id, dict(values)


def dispatch(provider, recipient, values, text, identifier):
    # Never use an incoming chat_id or fixed group webhook for private material.
    import json
    from . import im
    token = im_settings._active.set(values)
    try:
        class Client:
            def post(self, url, **kwargs):
                data = pa.request('POST', url, **kwargs)
                class Response:
                    def raise_for_status(self):
                        pass
                    def json(self):
                        return data
                return Response()
        client = Client()
        access = im.access_token(client, provider)
        if provider == 'feishu':
            im._post(client, 'https://open.feishu.cn/open-apis/im/v1/messages',
                params={'receive_id_type': 'open_id'}, headers={'Authorization': 'Bearer ' + access},
                json={'receive_id': recipient, 'msg_type': 'text', 'content': json.dumps({'text': text}), 'uuid': identifier})
        else:
            if values.get('DINGTALK_TRANSPORT') != 'stream':
                raise ValueError('legacy_private_unsupported')
            data = im._post(client, 'https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend',
                headers={'x-acs-dingtalk-access-token': access}, json={'robotCode': values['DINGTALK_ROBOT_CODE'],
                'userIds': [recipient], 'msgKey': 'sampleText', 'msgParam': json.dumps({'content': text}, ensure_ascii=False)})
            if not data.get('processQueryKey') or any(data.get(k) for k in ('invalidStaffIdList', 'flowControlledStaffIdList', 'filteredStaffIdList')):
                raise ValueError('recipient_rejected')
    finally:
        im_settings._active.reset(token)
