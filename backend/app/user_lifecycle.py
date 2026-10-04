"""Renaming, deactivating and reactivating users.

Deactivation cuts every live path to the user's access at once. Their history, group membership and IM identity
are kept: the identity must keep resolving to an inactive user, otherwise the same person would show up as a new
sender and could be onboarded a second time. Reactivation restores access but not personal platform authorization.
"""
from fastapi import HTTPException
from sqlalchemy import select
from . import approvals, policy, service
from . import platform_auth as pa
from .im_nicknames import clean
from .models import PlatformAuthJob, PlatformConnection, Run, SessionToken, User, uid

ACTIVE_RUNS = ('queued', 'running', 'waiting_attachments')


def manageable(db, actor, identifier):
    # Row lock: concurrent changes to the same user are applied one after the other, each seeing the other's result.
    target = db.scalar(select(User).where(User.id == identifier).with_for_update().execution_options(populate_existing=True))
    if target is None:
        raise HTTPException(404, 'Not found')
    policy.require(policy.can_manage_user(actor, target), 'Can only manage lower-ranked users in your scope')
    return target


def rename(db, actor, target, name):
    name = clean(name)
    if not name:
        raise HTTPException(400, 'Member name required')
    if name == target.name:
        return False
    target.name = name
    service.audit(db, actor, 'user.rename', target.id)  # Names stay out of the audit trail.
    return True


def deactivate(db, actor, target):
    if not target.active:
        return False
    target.active = False
    sessions = list(db.scalars(select(SessionToken).where(SessionToken.user_id == target.id)))
    for session in sessions:
        db.delete(session)
    runs = list(db.scalars(select(Run).where(Run.user_id == target.id, Run.status.in_(ACTIVE_RUNS)).with_for_update()))
    for run in runs:
        # Same effect as /stop: an answer produced afterwards is discarded and the run's tools stop working.
        run.status, run.error = 'cancelled', 'Cancelled: user deactivated'
        service.audit(db, actor, 'run.cancelled', run.id, {'source': 'deactivation'})
    cleared = []
    for provider in list(db.scalars(select(PlatformConnection.provider).where(PlatformConnection.user_id == target.id))):
        row = pa.locked(db, target.id, provider)
        if row.encrypted or row.state != 'disconnected':
            cleared.append(provider)
        row.state, row.encrypted, row.expires_at, row.next_poll_at = 'disconnected', '', None, None
        job = db.get(PlatformAuthJob, (target.id, provider))
        if job:
            job.generation, job.phase, job.notification = uid(), 'done', 'cancelled'
    service.audit(db, actor, 'user.deactivate', target.id, {
        'sessions_revoked': len(sessions), 'runs_cancelled': len(runs), 'connections_cleared': cleared,
        'approvals_closed': approvals.cancel_all(db, target)})
    return True


def activate(db, actor, target):
    if target.active:
        return False
    target.active = True
    service.audit(db, actor, 'user.activate', target.id)
    return True


def apply(db, actor, target, body):
    changed = False
    if body.name is not None:
        changed = rename(db, actor, target, body.name) or changed
    if body.active is True:
        changed = activate(db, actor, target) or changed
    elif body.active is False:
        changed = deactivate(db, actor, target) or changed
    db.flush()
    return changed
