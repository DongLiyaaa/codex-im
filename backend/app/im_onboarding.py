"""Opt-in automatic onboarding: a verified same-organization private sender becomes an IM-only member.

Off until a super administrator turns it on. It only ever acts on a first private message from a sender that the
platform vouches for as belonging to the application's own organization, creates a plain member in the one
organization and department the administrator chose, and stops at a daily cap. Every other case, including any
failure along the way, leaves the sender in the discovery list for a manual decision.
"""
import logging
from datetime import timedelta
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from . import im_discovery, schemas, service
from .db import get_db
from .im_nicknames import clean
from .models import Audit, Department, IMOnboardingPolicy, Identity, Organization, now

router = APIRouter(prefix='/api/im/onboarding-policy', tags=['im'])
log = logging.getLogger(__name__)
PROVIDERS = ('feishu', 'dingtalk')
LABELS = {'feishu': '飞书', 'dingtalk': '钉钉'}
DEFAULT_CAP, MAX_CAP, WINDOW = 20, 200, timedelta(hours=24)
# Automatic onboarding has no human actor; it acts with the rank needed to create a plain member and nothing else.
SYSTEM = SimpleNamespace(id=None, role='super_admin', active=True, org_id=None, team_id=None)


def action(provider):
    return 'im.auto_onboard.' + provider


def used(db, provider):
    return db.scalar(select(func.count()).select_from(Audit).where(Audit.action == action(provider),
                                                                   Audit.created_at > now() - WINDOW)) or 0


def valid(db, row):
    try:
        im_discovery.check_scope(db, SYSTEM, row.org_id, row.team_id)
    except HTTPException:
        return False
    return True


def placeholder(provider, sender):
    # Feishu events carry no display name; an administrator can rename the member afterwards.
    return f'{LABELS[provider]}用户 {sender[-6:]}'


def auto_onboard(db, provider, app_scope, sender, nickname):
    """Returns the new member, or None to leave the sender for manual approval.

    Everything, including reading the policy, runs inside one savepoint: whatever goes wrong here (a missing table
    during a rolling restart, an archived organization, a unique violation...) is undone and must never break the
    handling of the message itself.
    """
    try:
        with db.begin_nested():
            row = db.get(IMOnboardingPolicy, provider)
            if row is None or not row.enabled or not row.org_id or not row.team_id:
                return None
            im_discovery.configuration_lock(db, provider)  # Serializes the cap check with every other onboarding path.
            if used(db, provider) >= row.daily_cap:
                return None
            data = im_discovery.NewMember(name=clean(nickname) or placeholder(provider, sender), role='member',
                                          org_id=row.org_id, team_id=row.team_id)
            member = im_discovery.create_member(db, SYSTEM, data, via='im_auto')
            identity = Identity(provider=provider, external_user_id=sender, user_id=member.id)
            db.add(identity)
            db.flush()
            im_discovery.pin(db, 'identity', identity.id, app_scope)
            service.audit(db, None, action(provider), member.id, {'org_id': row.org_id, 'team_id': row.team_id})
            return member
    except (HTTPException, SQLAlchemyError, ValueError):
        log.warning('Automatic onboarding fell back to manual approval (%s)', provider)
        return None


class PolicyBody(schemas.Input):
    enabled: bool
    org_id: str | None = Field(default=None, min_length=1, max_length=100)
    team_id: str | None = Field(default=None, min_length=1, max_length=100)
    daily_cap: int = Field(default=DEFAULT_CAP, ge=1, le=MAX_CAP)

    @model_validator(mode='after')
    def complete(self):
        if self.enabled and not (self.org_id and self.team_id):
            raise ValueError('Organization and department required')
        if bool(self.org_id) != bool(self.team_id):
            raise ValueError('Organization and department go together')
        return self


def view(db, provider, row):
    org = db.get(Organization, row.org_id) if row and row.org_id else None
    team = db.get(Department, row.team_id) if row and row.team_id else None
    return {'provider': provider, 'enabled': bool(row and row.enabled), 'org_id': row.org_id if row else None,
            'team_id': row.team_id if row else None, 'org_name': org.name if org else None,
            'team_name': team.name if team else None, 'daily_cap': row.daily_cap if row else DEFAULT_CAP,
            'used_24h': used(db, provider), 'valid': valid(db, row) if row and row.enabled else None,
            'updated_at': row.updated_at if row else None}


@router.get('')
def policies(actor=Depends(im_discovery.admin), db=Depends(get_db, scope='function')):
    return [view(db, provider, db.get(IMOnboardingPolicy, provider)) for provider in PROVIDERS]


@router.put('/{provider}')
def save(provider: Literal['feishu', 'dingtalk'], body: PolicyBody, actor=Depends(im_discovery.admin), db=Depends(get_db, scope='function')):
    im_discovery.configuration_lock(db, provider)
    if body.enabled:
        # Only an enabled policy has to point at live directory entries; switching one off must always work.
        im_discovery.check_scope(db, actor, body.org_id, body.team_id)
    row = db.get(IMOnboardingPolicy, provider)
    if row is None:
        row = IMOnboardingPolicy(provider=provider)
        db.add(row)
    row.enabled, row.org_id, row.team_id, row.daily_cap = body.enabled, body.org_id, body.team_id, body.daily_cap
    row.updated_by, row.updated_at = actor.id, now()
    db.flush()
    service.audit(db, actor, 'im.onboarding_policy.update', provider, {
        'enabled': row.enabled, 'org_id': row.org_id, 'team_id': row.team_id, 'daily_cap': row.daily_cap})
    return view(db, provider, row)
