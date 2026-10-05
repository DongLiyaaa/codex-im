"""Verified ingress metadata and explicit, scoped IM onboarding."""
import hashlib
import json
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from . import im_settings, policy, schemas, service
from .db import get_db
from .models import IM_ONLY_EMAIL_DOMAIN, LOCKED_PASSWORD, IMDiscovery, IMChatName, IMScopeBinding, Identity, Group, User, now, uid
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


def remember_chat_name(db, provider, app_scope, chat, name):
    # Caller holds the discovery advisory lock; names are display metadata only.
    from .im_nicknames import clean
    name = clean(name)
    if not name:
        return
    keys = dict(provider=provider, app_scope=app_scope, chat_id=chat)
    if not db.get(IMChatName, (provider, app_scope, chat)) and db.scalar(select(func.count()).select_from(IMChatName)) >= 10000:
        oldest = db.scalar(select(IMChatName).order_by(IMChatName.updated_at).limit(1))
        if oldest:
            db.delete(oldest)
            db.flush()
    statement = insert(IMChatName).values(**keys, name=name, updated_at=now())
    db.execute(statement.on_conflict_do_update(index_elements=list(keys),
        set_={'name': statement.excluded.name, 'updated_at': now()}))


def store_chat_names(db, provider, app_scope, names):
    # Same global discovery lock as record(), so bounded eviction never races ingress.
    db.scalar(select(func.pg_advisory_xact_lock(71903)))
    for chat, name in names.items():
        remember_chat_name(db, provider, app_scope, chat, name)


def chat_names(db, rows):
    chats = {row.chat_id for row in rows if row.chat_type == 'group'}
    if not chats:
        return {}
    return {(r.provider, r.app_scope, r.chat_id): r.name
            for r in db.scalars(select(IMChatName).where(IMChatName.chat_id.in_(sorted(chats))))}


def chat_name_status(row, names):
    if row.chat_type != 'group':
        return 'private_chat'
    if (row.provider, row.app_scope, row.chat_id) in names:
        return 'available'
    if row.provider != 'feishu':
        return 'not_provided'
    from .im_nicknames import chat_failure
    return chat_failure(row.app_scope, row.chat_id) or 'not_resolved'


def record(db, provider, app_scope, sender, chat, is_group, rejection, nickname=None, chat_name=None):
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
    if is_group and chat_name:
        remember_chat_name(db, provider, app_scope, chat, chat_name)


def admin(actor=Depends(current_user)):
    policy.require(actor.active and actor.role == 'super_admin')
    return actor


@router.get('')
def discoveries(actor=Depends(admin), db=Depends(get_db, scope='function')):
    result = []
    rows = list(db.scalars(select(IMDiscovery).order_by(IMDiscovery.last_seen.desc()).limit(500)))
    names = chat_names(db, rows)
    for row in rows:
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
            'current_reason': rejection, 'user_id': user.id if user else None, 'group_id': group.id if group else None,
            'nickname_status': nickname_status(row),
            'chat_name': names.get((row.provider, row.app_scope, row.chat_id)),
            'chat_name_status': chat_name_status(row, names)})
    return result


def nickname_status(row):
    if row.nickname:
        return 'available'
    if row.provider != 'feishu':
        return 'not_provided'
    from .im_nicknames import failure
    return failure(row.app_scope, row.sender_id) or 'not_resolved'


@router.post('/nicknames')
def refresh_nicknames(actor=Depends(admin), db=Depends(get_db, scope='function')):
    from .im_nicknames import refresh
    result = refresh(db)
    service.audit(db, actor, 'im.discovery.nickname_refresh', 'feishu',
                  {key: result[key] for key in ('status', 'resolved', 'unresolved', 'remaining',
                                                'chat_resolved', 'chat_unresolved', 'chat_remaining')})
    return result


def bound_users(db, provider, app_scope):
    return list(db.scalars(select(User).join(Identity, Identity.user_id == User.id)
        .join(IMScopeBinding, (IMScopeBinding.subject_id == Identity.id) &
              (IMScopeBinding.subject_type == 'identity') & (IMScopeBinding.app_scope == app_scope))
        .where(Identity.provider == provider, User.active.is_(True)).distinct().order_by(User.name, User.id)))


@router.get('/groups')
def discovered_groups(actor=Depends(admin), db=Depends(get_db, scope='function')):
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
        rows = list(rows)
        names = {r.chat_id: r.name for r in db.scalars(select(IMChatName).where(IMChatName.provider == provider,
            IMChatName.app_scope == current_scope, IMChatName.chat_id.in_(sorted({row.chat_id for row in rows}))))} if rows else {}
        for row in rows:
            result.append({'id': row.id, 'provider': provider, 'external_id': row.chat_id,
                'name': names.get(row.chat_id), 'first_seen': row.first_seen, 'last_seen': row.last_seen,
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
def bind_group(identifier: str, body: BindGroup, actor=Depends(admin), db=Depends(get_db, scope='function')):
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


class NewMember(schemas.Input):
    """Created while approving: an IM-only member with no email, password or web login.

    Only the two non-admin roles are offered; administrators need a real, deliberately created account.
    """
    name: str = Field(min_length=1, max_length=200)
    role: Literal['member', 'team_lead'] = 'member'
    org_id: str = Field(min_length=1, max_length=100)
    team_id: str = Field(min_length=1, max_length=100)


class Approve(schemas.Input):
    user_id: str | None = Field(default=None, min_length=1, max_length=36)
    new_user: NewMember | None = None
    group_id: str | None = Field(default=None, min_length=1, max_length=36)
    new_group: schemas.GroupCreate | None = None
    confirm_member: bool = False

    @model_validator(mode='after')
    def one_member(self):
        if (self.user_id is None) == (self.new_user is None):
            raise ValueError('Choose an existing user or create a new member')
        if self.new_user and (self.group_id or self.new_group or self.confirm_member):
            # The group's member list and scope are checked against a member that must already exist.
            raise ValueError('Register the group after the member is created')
        return self


def check_scope(db, actor, org_id, team_id):
    """The organization and department must be live directory entries, and the department must belong to the organization."""
    from .directory import catalog, validate_scope
    validate_scope(db, org_id, team_id)
    listing = catalog(db, actor)
    if not any(o['id'] == org_id for o in listing['organizations']):
        raise HTTPException(400, 'Unknown organization')
    if not any(d['id'] == team_id and d['org_id'] == org_id for d in listing['departments']):
        raise HTTPException(400, 'Unknown department')


def create_member(db, actor, data, via='im_discovery'):
    """Create the IM-only member inside the approval transaction; any later failure rolls it back with the rest."""
    from .im_nicknames import clean
    name = clean(data.name)
    if not name:
        raise HTTPException(400, 'Member name required')
    check_scope(db, actor, data.org_id, data.team_id)
    # An explicit id: the rank check below must never compare an unassigned id with an actor that has none.
    member = User(id=uid(), email=f'im-{uuid.uuid4().hex}@{IM_ONLY_EMAIL_DOMAIN}', name=name, password_hash=LOCKED_PASSWORD,
                  role=data.role, org_id=data.org_id, team_id=data.team_id, active=True)
    policy.require(policy.can_manage_user(actor, member), 'Can only create lower-ranked users in your scope')
    db.add(member)
    db.flush()
    service.audit(db, actor, 'user.create', member.id, {'via': via, 'login_enabled': False, 'role': member.role})
    return member


@router.post('/{identifier}/approve')
def approve(identifier: str, body: Approve, actor=Depends(admin), db=Depends(get_db, scope='function')):
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
    created = body.new_user is not None
    if created:
        identity = db.scalar(select(Identity).where(Identity.provider == row.provider, Identity.external_user_id == row.sender_id))
        if identity:
            raise HTTPException(409, 'External identity already bound; choose the existing user')
        user = create_member(db, actor, body.new_user)
    else:
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
        'app_scope': row.app_scope, 'created_user': created})
    return {'ok': True, 'user_id': user.id, 'group_id': group.id if group else None, 'resend_required': True,
            'created_user': created}


MAX_BATCH = 50


class BatchItem(schemas.Input):
    discovery_id: str = Field(min_length=1, max_length=36)
    name: str = Field(min_length=1, max_length=200)


class BatchOnboard(schemas.Input):
    """Several senders of one platform become IM-only members of the same organization, department and role."""
    items: list[BatchItem] = Field(min_length=1, max_length=MAX_BATCH)
    role: Literal['member', 'team_lead'] = 'member'
    org_id: str = Field(min_length=1, max_length=100)
    team_id: str = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def distinct(self):
        if len({item.discovery_id for item in self.items}) != len(self.items):
            raise ValueError('Duplicate discovery')
        return self


@router.post('/onboard-batch')
def onboard_batch(body: BatchOnboard, actor=Depends(admin), db=Depends(get_db, scope='function')):
    """Each sender is onboarded in its own savepoint: one conflict never blocks or undoes the others."""
    admin(actor)
    rows = {item.discovery_id: db.get(IMDiscovery, item.discovery_id) for item in body.items}
    providers = {row.provider for row in rows.values() if row}
    if len(providers) > 1:
        # One platform per request keeps the lock order (platform configuration, then directory) identical everywhere.
        raise HTTPException(400, 'Onboard one platform at a time')
    provider = next(iter(providers), None)
    if provider:
        configuration_lock(db, provider)
    from .directory import scope_lock
    scope_lock(db)
    results, seen = [], set()
    for item in body.items:
        row, entry = rows[item.discovery_id], {'id': item.discovery_id, 'ok': False}
        results.append(entry)
        if row is None:
            entry.update(error='Not found', status=404)
            continue
        if (row.provider, row.sender_id) in seen:
            entry.update(error='Duplicate sender in this batch', status=409)
            continue
        seen.add((row.provider, row.sender_id))
        member = NewMember(name=item.name, role=body.role, org_id=body.org_id, team_id=body.team_id)
        try:
            with db.begin_nested():
                done = approve(item.discovery_id, Approve(new_user=member), actor, db)
            entry.update(ok=True, user_id=done['user_id'])
        except HTTPException as exc:
            entry.update(error=str(exc.detail), status=exc.status_code)
        except IntegrityError:
            entry.update(error='Conflicting or invalid record', status=409)
    created = sum(1 for entry in results if entry['ok'])
    service.audit(db, actor, 'im.discovery.onboard_batch', provider, {'requested': len(results), 'created': created})
    return {'results': results, 'created': created, 'failed': len(results) - created}
