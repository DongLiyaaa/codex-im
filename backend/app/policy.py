from fastapi import HTTPException
from sqlalchemy import select
from .models import User, Group, Resource, Binding

RANK = {'member': 0, 'team_lead': 1, 'org_admin': 2, 'super_admin': 3}


def require(value, detail='Forbidden'):
    if not value:
        raise HTTPException(403, detail)


def can_manage_user(actor, target):
    if not actor.active or actor.id == target.id or RANK[actor.role] <= RANK[target.role]:
        return False
    if actor.role == 'super_admin':
        return True
    if not actor.org_id or actor.org_id != target.org_id:
        return False
    return actor.role == 'org_admin' or (
        actor.role == 'team_lead' and actor.team_id is not None and actor.team_id == target.team_id
        and target.role == 'member')


def can_manage_group(db, actor, group):
    if not group or getattr(group, 'archived_at', None) is not None or actor.role == 'member' or not actor.active:
        return False
    if actor.role != 'super_admin' and actor.org_id != group.org_id:
        return False
    if actor.role == 'team_lead' and actor.team_id != group.team_id:
        return False
    members = [db.get(User, item) for item in group.member_ids]
    return bool(members) and all(member and (member.id == actor.id or can_manage_user(actor, member)) for member in members)


def in_group_scope(user, group):
    return bool(group and getattr(group, 'archived_at', None) is None and user and user.active and (user.role == 'super_admin' or
        (user.org_id == group.org_id and (not group.team_id or user.team_id == group.team_id))))


def is_group_member(user, group):
    return bool(group and in_group_scope(user, group) and user.id in group.member_ids)


def can_read_group(db, actor, group):
    return actor.active and (is_group_member(actor, group) or can_manage_group(db, actor, group))


def can_read_conversation(db, actor, conversation):
    if not actor.active or conversation.archived_at is not None:
        return False
    if conversation.group_id:
        group = db.get(Group, conversation.group_id)
        return bool(group and can_read_group(db, actor, group))
    owner = db.get(User, conversation.owner_id)
    return bool(owner and (actor.id == owner.id or can_manage_user(actor, owner)))


def can_send_conversation(db, actor, conversation):
    if not actor.active or conversation.archived_at is not None:
        return False
    if conversation.group_id:
        group = db.get(Group, conversation.group_id)
        return is_group_member(actor, group)
    return actor.id == conversation.owner_id


def can_delete_conversation(db, actor, conversation):
    if not actor.active or conversation.archived_at is not None:
        return False
    if conversation.group_id:
        group = db.get(Group, conversation.group_id)
        return bool(group and can_manage_group(db, actor, group))
    return actor.id == conversation.owner_id


def can_manage_resource(actor, resource):
    return actor.active and (actor.role == 'super_admin' or (
        actor.role == 'org_admin' and actor.org_id is not None and actor.org_id == resource.org_id))


def binding_subject(db, binding):
    return db.get(User if binding.subject_type == 'user' else Group, binding.subject_id)


def can_manage_binding(db, actor, binding):
    subject = binding_subject(db, binding)
    resource = db.get(Resource, binding.resource_id)
    if not subject or not resource:
        return False
    scope_ok = (resource.org_id is None or resource.org_id == subject.org_id or
                (binding.subject_type == 'user' and subject.role == 'super_admin' and
                 actor.role == 'super_admin' and actor.id == subject.id))
    subject_ok = (actor.id == subject.id or can_manage_user(actor, subject)) if binding.subject_type == 'user' else can_manage_group(db, actor, subject)
    # Delegation of grants requires resource administration as well as subject scope.
    return scope_ok and subject_ok and can_manage_resource(actor, resource)


def effective_resources(db, user, conversation):
    if not can_send_conversation(db, user, conversation):
        return []
    ids = set(db.scalars(select(Binding.resource_id).where(Binding.subject_type == 'user', Binding.subject_id == user.id)))
    if conversation.group_id:
        group_ids = set(db.scalars(select(Binding.resource_id).where(Binding.subject_type == 'group', Binding.subject_id == conversation.group_id)))
        ids &= group_ids
    if not ids:
        return []
    resource_org = db.get(Group, conversation.group_id).org_id if conversation.group_id else user.org_id
    return [r for r in db.scalars(select(Resource).where(Resource.id.in_(ids), Resource.enabled.is_(True)))
            if r.org_id is None or r.org_id == resource_org]
