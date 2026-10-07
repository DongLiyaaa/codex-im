"""Hub-side 可用人员 for personal platform authorization.

DingTalk's CLI 可用人员 list can only be edited in its developer console (there is no API for it), so the organization
opens CLI data access to everyone there and the super administrator picks the people here instead. Until a list is
saved everyone may authorize, as before this existed. Removing someone discards their stored authorization at once.
"""
from typing import Literal

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import func, select

from . import platform_auth as pa, schemas
from .models import (IM_ONLY_EMAIL_DOMAIN, Department, Identity, Organization, PlatformAccess, PlatformAuthJob,
                     PlatformConnection, User, now, uid)

MAX_USERS = 1000
ACTIVE = ('pending', 'connected', 'starting')


class Update(schemas.Input):
    revision: int = Field(ge=0)
    user_scope: Literal['all', 'specified']
    user_ids: list[str] = Field(default_factory=list, max_length=MAX_USERS)


def allowed(db, user, provider):
    row = db.get(PlatformAccess, provider)
    return row is None or row.user_scope != 'specified' or user.id in (row.user_ids or [])


def view(db, provider):
    pa.check_provider(provider)
    row = db.get(PlatformAccess, provider)
    return {'provider': provider, 'revision': row.revision if row else 0,
            'user_scope': row.user_scope if row else 'all', 'user_ids': list(row.user_ids or []) if row else [],
            'people': people(db, provider)}


def people(db, provider):
    """Everyone the list can name, as an administrator recognizes them: organization, department, the platform
    account bound to them (only bound people can authorize) and where their own authorization stands."""
    orgs = dict(db.execute(select(Organization.id, Organization.name)).all())
    teams = dict(db.execute(select(Department.id, Department.name)).all())
    accounts = dict(db.execute(select(Identity.user_id, Identity.external_user_id)
                               .where(Identity.provider == provider).order_by(Identity.external_user_id)).all())
    states = dict(db.execute(select(PlatformConnection.user_id, PlatformConnection.state)
                             .where(PlatformConnection.provider == provider)).all())
    result = [{'id': u.id, 'name': u.name, 'role': u.role, 'active': u.active,
               # IM-only members carry a reserved placeholder address that means nothing to a person.
               'email': None if u.email.endswith('@' + IM_ONLY_EMAIL_DOMAIN) else u.email,
               'organization': orgs.get(u.org_id, u.org_id) if u.org_id else None,
               'department': teams.get(u.team_id, u.team_id) if u.team_id else None,
               'account': accounts.get(u.id), 'authorization': states.get(u.id)} for u in db.scalars(select(User))]
    return sorted(result, key=lambda p: (p['account'] is None, not p['active'], p['organization'] or '',
                                         p['department'] or '', p['name']))


def save(db, actor, provider, body):
    pa.check_provider(provider)
    db.execute(select(func.pg_advisory_xact_lock(71921 if provider == 'feishu' else 71922)))
    row = db.get(PlatformAccess, provider)
    if (row.revision if row else 0) != body.revision:
        raise HTTPException(409, 'Access list changed; reload and try again')
    ids = list(dict.fromkeys(body.user_ids)) if body.user_scope == 'specified' else []
    known = set(db.scalars(select(User.id).where(User.id.in_(ids)))) if ids else set()
    if len(known) != len(ids):
        raise HTTPException(422, 'Unknown user')
    if row is None:
        row = PlatformAccess(provider=provider, revision=0)
        db.add(row)
    row.user_scope, row.user_ids, row.revision = body.user_scope, ids, row.revision + 1
    row.updated_by, row.updated_at = actor.id, now()
    db.flush()
    revoked = []
    if body.user_scope == 'specified':
        holders = db.scalars(select(PlatformConnection.user_id).where(PlatformConnection.provider == provider,
            PlatformConnection.state.in_(ACTIVE), PlatformConnection.user_id.not_in(ids or [''])).order_by(PlatformConnection.user_id))
        for user_id in list(holders):
            connection = pa.locked(db, user_id, provider)
            if connection.state in ACTIVE:
                revoke(db, connection)
                revoked.append(user_id)
    from .service import audit
    audit(db, actor, 'platform.access.update', provider, {'revision': row.revision, 'user_scope': row.user_scope,
                                                          'users': len(ids), 'revoked': len(revoked)})
    return view(db, provider)


def revoke(db, connection):
    connection.state, connection.encrypted, connection.expires_at, connection.next_poll_at = 'user_not_allowed', '', None, None
    job = db.get(PlatformAuthJob, (connection.user_id, connection.provider))
    if job:
        # A new generation makes any in-flight worker step discard its result.
        job.generation, job.phase, job.notification, job.error = uid(), 'done', 'cancelled', 'USER_NOT_ALLOWED'
