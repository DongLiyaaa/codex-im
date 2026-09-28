"""Verified ingress metadata and explicit, scoped IM onboarding."""
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert
from . import im_settings, policy, schemas, service
from .db import get_db
from .models import IMDiscovery, IMScopeBinding, Identity, Group, User, now
from .security import current_user

router = APIRouter(prefix='/api/im/discoveries', tags=['im'])


def scope(provider):
    im_settings.provider_check(provider)
    names = ['FEISHU_APP_ID'] if provider == 'feishu' else ['DINGTALK_CLIENT_ID', 'DINGTALK_ROBOT_CODE']
    values = [im_settings.value(name) for name in names]
    if not all(values):
        raise HTTPException(503, 'IM application identity unavailable')
    return hashlib.sha256(json.dumps([provider, *values], ensure_ascii=False).encode()).hexdigest()


def configuration_lock(db, provider):
    db.execute(select(func.pg_advisory_xact_lock(71901 if provider == 'feishu' else 71902)))


def pinned(db, kind, identifier, app_scope):
    return db.scalar(select(IMScopeBinding.id).where(IMScopeBinding.subject_type == kind,
        IMScopeBinding.subject_id == identifier, IMScopeBinding.app_scope == app_scope)) is not None


def pin(db, kind, identifier, app_scope):
    row = db.scalar(select(IMScopeBinding).where(IMScopeBinding.subject_type == kind,
        IMScopeBinding.subject_id == identifier).with_for_update())
    if row and row.app_scope != app_scope:
        raise HTTPException(409, 'Mapping belongs to another application')
    if not row:
        db.add(IMScopeBinding(subject_type=kind, subject_id=identifier, app_scope=app_scope))
        db.flush()


def reason(db, provider, app_scope, sender, chat, is_group):
    identity = db.scalar(select(Identity).where(Identity.provider == provider, Identity.external_user_id == sender))
    user = db.get(User, identity.user_id) if identity else None
    group = db.scalar(select(Group).where(Group.provider == provider, Group.external_id == chat,
        Group.archived_at.is_(None)).with_for_update().execution_options(populate_existing=True)) if is_group else None
    if not identity:
        return 'unknown_sender', user, group
    if not pinned(db, 'identity', identity.id, app_scope):
        return 'identity_scope_mismatch', user, group
    if not user or not user.active:
        return 'inactive_user', user, group
    if is_group:
        if not group:
            return 'unknown_group', user, group
        if not pinned(db, 'group', group.id, app_scope):
            return 'group_scope_mismatch', user, group
        if not policy.in_group_scope(user, group):
            return 'outside_group_scope', user, group
        if user.id not in group.member_ids:
            return 'not_member', user, group
    return None, user, group


def record(db, provider, app_scope, sender, chat, is_group, rejection, nickname=None):
    # Global bounded metadata set; event tombstones are separate and never evicted.
    db.execute(select(func.pg_advisory_xact_lock(71903)))
    keys = dict(provider=provider, app_scope=app_scope, sender_id=sender, chat_id=chat,
                chat_type='group' if is_group else 'p2p')
    existing = db.scalar(select(IMDiscovery.id).filter_by(**keys))
    if not existing and db.scalar(select(func.count()).select_from(IMDiscovery)) >= 10000:
        oldest = select(IMDiscovery.id).order_by(IMDiscovery.last_seen, IMDiscovery.id).limit(1)
        db.execute(delete(IMDiscovery).where(IMDiscovery.id.in_(oldest)))
    nickname = nickname[:200] if isinstance(nickname, str) else None
    statement = insert(IMDiscovery).values(**keys, reason=rejection, nickname=nickname)
    db.execute(statement.on_conflict_do_update(index_elements=list(keys), set_={
        'last_seen': now(), 'reason': rejection,
        'nickname': func.coalesce(statement.excluded.nickname, IMDiscovery.nickname)}))


def admin(actor=Depends(current_user)):
    policy.require(actor.active and actor.role == 'super_admin')
    return actor


@router.get('')
def discoveries(actor=Depends(admin), db=Depends(get_db)):
    result = []
    for row in db.scalars(select(IMDiscovery).order_by(IMDiscovery.last_seen.desc()).limit(500)):
        with im_settings.snapshot(db, row.provider):
            try:
                current = scope(row.provider) == row.app_scope
            except HTTPException:
                current = False
            rejection, user, group = reason(db, row.provider, row.app_scope, row.sender_id, row.chat_id, row.chat_type == 'group')
            if not rejection and row.provider == 'dingtalk' and im_settings.value('DINGTALK_TRANSPORT', 'webhook') == 'webhook':
                if row.chat_type != 'group' or row.chat_id != im_settings.value('DINGTALK_ROBOT_CHAT_ID'):
                    rejection = 'unsupported_reply_target'
        result.append({key: getattr(row, key) for key in ('id', 'provider', 'app_scope', 'sender_id', 'chat_id',
            'chat_type', 'nickname', 'first_seen', 'last_seen', 'reason')} | {
            'status': 'stale_application' if not current else 'pending' if rejection else 'authorized',
            'current_reason': rejection, 'user_id': user.id if user else None, 'group_id': group.id if group else None})
    return result


def bound_users(db, provider, app_scope):
    return list(db.scalars(select(User).join(Identity, Identity.user_id == User.id)
        .join(IMScopeBinding, (IMScopeBinding.subject_id == Identity.id) &
              (IMScopeBinding.subject_type == 'identity') & (IMScopeBinding.app_scope == app_scope))
        .where(Identity.provider == provider, User.active.is_(True)).distinct().order_by(User.name, User.id)))


@router.get('/groups')
def discovered_groups(actor=Depends(admin), db=Depends(get_db)):
    admin(actor)
    result = []
    for provider in ('feishu', 'dingtalk'):
        with im_settings.snapshot(db, provider):
            try:
                current_scope = scope(provider)
            except HTTPException:
                continue
        linked = select(Group.id).where(Group.provider == IMDiscovery.provider,
                                        Group.external_id == IMDiscovery.chat_id, Group.archived_at.is_(None)).exists()
        rows = db.execute(select(IMDiscovery.chat_id, func.min(IMDiscovery.id).label('id'),
            func.min(IMDiscovery.first_seen).label('first_seen'), func.max(IMDiscovery.last_seen).label('last_seen'))
            .where(IMDiscovery.provider == provider, IMDiscovery.app_scope == current_scope,
                   IMDiscovery.chat_type == 'group', ~linked)
            .group_by(IMDiscovery.chat_id))
        members = [u for u in bound_users(db, provider, current_scope)
                   if u.id == actor.id or policy.can_manage_user(actor, u)]
        candidates = [{'id': u.id, 'name': u.name, 'org_id': u.org_id, 'team_id': u.team_id, 'role': u.role} for u in members]
        for row in rows:
            result.append({'id': row.id, 'provider': provider, 'external_id': row.chat_id,
                'name': None, 'first_seen': row.first_seen, 'last_seen': row.last_seen,
                'members': candidates})
    return sorted(result, key=lambda row: (row['last_seen'], row['id']), reverse=True)


class BindGroup(schemas.Input):
    group_id: str | None = Field(default=None, min_length=1, max_length=36)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    org_id: str | None = Field(default=None, max_length=100)
    team_id: str | None = Field(default=None, max_length=100)
    member_ids: list[str] = Field(default_factory=list, max_length=1000)
    confirm_members: bool = False


@router.post('/groups/{identifier}/bind')
def bind_group(identifier: str, body: BindGroup, actor=Depends(admin), db=Depends(get_db)):
    admin(actor)
    row = db.get(IMDiscovery, identifier)
    if not row or row.chat_type != 'group':
        raise HTTPException(404, 'Discovered group not found')
    configuration_lock(db, row.provider)
    from .directory import scope_lock
    scope_lock(db)
    db.refresh(row)
    with im_settings.snapshot(db, row.provider):
        if scope(row.provider) != row.app_scope:
            raise HTTPException(409, 'Application changed; refresh discovered groups')
    if not body.confirm_members:
        raise HTTPException(400, 'Explicit membership confirmation required')
    if db.scalar(select(Group.id).where(Group.provider == row.provider, Group.external_id == row.chat_id, Group.archived_at.is_(None))):
        raise HTTPException(409, 'Group already linked; refresh the group list')
    eligible = {u.id: u for u in bound_users(db, row.provider, row.app_scope)}
    if body.group_id:
        group = service.lock_group(db, body.group_id)
        policy.require(group and policy.can_manage_group(db, actor, group))
        service.require_idle_group(db, group.id)
        if group.external_id or group.provider not in ('web', row.provider):
            raise HTTPException(409, 'Group already linked or platform conflicts')
        if body.member_ids or body.name or body.org_id or body.team_id:
            raise HTTPException(400, 'Existing group retains its scope and members')
    else:
        members = list(dict.fromkeys(body.member_ids))
        if not members or not body.org_id or not body.name or not body.name.strip():
            raise HTTPException(400, 'Choose members, organization and group name')
        group = Group(name=body.name.strip(), org_id=body.org_id, team_id=body.team_id,
                      member_ids=members)
    from .directory import validate_scope
    validate_scope(db, group.org_id, group.team_id)
    for uid in group.member_ids:
        member = eligible.get(uid)
        policy.require(policy.in_group_scope(member, group), 'Choose identities bound in this application and group scope')
    policy.require(policy.can_manage_group(db, actor, group))
    group.provider, group.external_id = row.provider, row.chat_id
    db.add(group)
    db.flush()
    pin(db, 'group', group.id, row.app_scope)
    service.audit(db, actor, 'im.group.bind', group.id, {'discovery_id': row.id,
        'member_ids': group.member_ids, 'app_scope': row.app_scope})
    return {'ok': True, 'group_id': group.id}


class Approve(schemas.Input):
    user_id: str = Field(min_length=1, max_length=36)
    group_id: str | None = Field(default=None, min_length=1, max_length=36)
    new_group: schemas.GroupCreate | None = None
    confirm_member: bool = False


@router.post('/{identifier}/approve')
def approve(identifier: str, body: Approve, actor=Depends(admin), db=Depends(get_db)):
    # Enforce authorization even for direct callers; all writes commit atomically.
    admin(actor)
    row = db.get(IMDiscovery, identifier)
    if not row:
        raise HTTPException(404, 'Not found')
    configuration_lock(db, row.provider)
    from .directory import scope_lock
    scope_lock(db)
    db.refresh(row)
    with im_settings.snapshot(db, row.provider):
        if scope(row.provider) != row.app_scope:
            raise HTTPException(409, 'Application changed; discover a new message')
    user = db.get(User, body.user_id)
    policy.require(user and user.active and (policy.can_manage_user(actor, user) or user.id == actor.id))
    identity = db.scalar(select(Identity).where(Identity.provider == row.provider, Identity.external_user_id == row.sender_id))
    if identity and (identity.user_id != user.id or not pinned(db, 'identity', identity.id, row.app_scope)):
        raise HTTPException(409, 'External identity already bound or application ownership unknown')
    group = None
    if body.group_id and body.new_group:
        raise HTTPException(400, 'Choose one group')
    if row.chat_type != 'group' and (body.group_id or body.new_group or body.confirm_member):
        raise HTTPException(400, 'Private conversation has no group')
    if body.group_id or body.new_group:
        if not body.confirm_member:
            raise HTTPException(400, 'Explicit membership confirmation required')
        if body.new_group:
            data = body.new_group
            if data.provider != row.provider or data.external_id != row.chat_id or user.id not in data.member_ids:
                raise HTTPException(400, 'Group must match discovery and selected member')
            group = Group(**data.model_dump())
        else:
            group = service.lock_group(db, body.group_id)
            policy.require(group and policy.can_manage_group(db, actor, group))
            service.require_idle_group(db, group.id)
            if group.provider != row.provider or (group.external_id and
                    (group.external_id != row.chat_id or not pinned(db, 'group', group.id, row.app_scope))):
                raise HTTPException(409, 'Group platform or external mapping conflicts')
            group.member_ids = list(dict.fromkeys([*group.member_ids, user.id]))
            group.external_id = row.chat_id
        from .directory import validate_scope
        validate_scope(db, group.org_id, group.team_id)
        for member_id in group.member_ids:
            member = db.get(User, member_id)
            policy.require(policy.in_group_scope(member, group), 'Member outside group scope')
        policy.require(policy.can_manage_group(db, actor, group))
        db.add(group)
        db.flush()
        pin(db, 'group', group.id, row.app_scope)
    if not identity:
        identity = Identity(provider=row.provider, external_user_id=row.sender_id, user_id=user.id)
        db.add(identity)
        db.flush()
    pin(db, 'identity', identity.id, row.app_scope)
    service.audit(db, actor, 'im.discovery.approve', row.id, {'user_id': user.id,
        'group_id': group.id if group else None, 'confirmed_member': body.confirm_member,
        'app_scope': row.app_scope})
    return {'ok': True, 'user_id': user.id, 'group_id': group.id if group else None, 'resend_required': True}
