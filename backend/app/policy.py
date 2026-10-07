from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from .models import User, Group, Resource, Binding, Organization, Department

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


def in_resource_scope(resource, org_id, team_id):
    """Global resources reach everyone, an organization's resources its members, a department's resources only that
    department. An organization-level group (no department) therefore cannot use a department's resource."""
    return ((resource.org_id is None or resource.org_id == org_id) and
            (getattr(resource, 'team_id', None) is None or resource.team_id == team_id))


class GrantScope:
    """A whole organization or department as a grant subject, carrying the scope its grants are checked against."""

    def __init__(self, org_id, team_id=None):
        self.org_id, self.team_id = org_id, team_id


def binding_subject(db, binding):
    if binding.subject_type == 'org':
        row = db.get(Organization, binding.subject_id)
        return GrantScope(row.id) if row and row.archived_at is None else None
    if binding.subject_type == 'team':
        row = db.get(Department, binding.subject_id)
        return GrantScope(row.org_id, row.id) if row and row.archived_at is None else None
    return db.get(User if binding.subject_type == 'user' else Group, binding.subject_id)


def can_manage_binding(db, actor, binding):
    subject = binding_subject(db, binding)
    resource = db.get(Resource, binding.resource_id)
    if not subject or not resource:
        return False
    scope_ok = (in_resource_scope(resource, subject.org_id, subject.team_id) or
                (binding.subject_type == 'user' and subject.role == 'super_admin' and
                 actor.role == 'super_admin' and actor.id == subject.id))
    if binding.subject_type == 'user':
        subject_ok = actor.id == subject.id or can_manage_user(actor, subject)
    elif binding.subject_type == 'group':
        subject_ok = can_manage_group(db, actor, subject)
    else:
        # Granting to a whole organization or department is an administrator's decision for that organization.
        subject_ok = actor.active and (actor.role == 'super_admin' or (
            actor.role == 'org_admin' and actor.org_id is not None and actor.org_id == subject.org_id))
    # Delegation of grants requires resource administration as well as subject scope.
    return scope_ok and subject_ok and can_manage_resource(actor, resource)


def subject_keys(db, owner, kind):
    """The grant subjects that reach a user or a group: itself, its department and its organization."""
    keys = [(kind, owner.id)]
    org = db.get(Organization, owner.org_id) if owner.org_id else None
    if org and org.archived_at is None:
        keys.append(('org', org.id))
    team = db.get(Department, owner.team_id) if owner.org_id and owner.team_id else None
    if team and team.archived_at is None and team.org_id == owner.org_id:
        keys.append(('team', team.id))
    return keys


def granted_ids(db, keys):
    return set(db.scalars(select(Binding.resource_id).where(
        or_(*(and_(Binding.subject_type == kind, Binding.subject_id == identifier) for kind, identifier in keys)))))


def effective_resources(db, user, conversation):
    if not can_send_conversation(db, user, conversation):
        return []
    ids = granted_ids(db, subject_keys(db, user, 'user'))
    owner = db.get(Group, conversation.group_id) if conversation.group_id else user
    if conversation.group_id:
        ids &= granted_ids(db, subject_keys(db, owner, 'group'))
    if not ids:
        return []
    return [r for r in db.scalars(select(Resource).where(Resource.id.in_(ids), Resource.enabled.is_(True)))
            if in_resource_scope(r, owner.org_id, owner.team_id)]
