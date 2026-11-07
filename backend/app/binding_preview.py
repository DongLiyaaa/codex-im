from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from . import policy
from .db import get_db
from .models import Binding, Group, Resource, User
from .security import current_user

router = APIRouter(tags=['bindings'])


class Source(BaseModel):
    side: Literal['user', 'group']
    subject_type: str
    subject_id: str


class PreviewResource(BaseModel):
    id: str
    name: str
    kind: str
    enabled: bool
    effective: bool
    reasons: list[str]
    sources: list[Source]


class Preview(BaseModel):
    user_id: str
    group_id: str | None
    can_send: bool
    context_reasons: list[str]
    resources: list[PreviewResource]


def evaluate(db, actor, user_id, group_id=None):
    policy.require(actor.active and actor.role in ('super_admin', 'org_admin'))
    user = db.get(User, user_id)
    if not user or not (actor.id == user.id or policy.can_manage_user(actor, user)):
        raise HTTPException(404, '预览用户不存在或无权查看。')
    group = db.get(Group, group_id) if group_id else None
    if group_id and (not group or not policy.can_read_group(db, actor, group)):
        raise HTTPException(404, '预览群组不存在或无权查看。')
    context = SimpleNamespace(owner_id=user.id, group_id=group_id, archived_at=None)
    can_send = policy.can_send_conversation(db, user, context)
    context_reasons = []
    if not user.active:
        context_reasons.append('inactive_user')
    if group:
        if group.archived_at:
            context_reasons.append('archived_group')
        if user.id not in group.member_ids:
            context_reasons.append('not_group_member')
        if user.active and not policy.in_group_scope(user, group):
            context_reasons.append('outside_group_scope')
    user_keys = set(policy.subject_keys(db, user, 'user'))
    group_keys = set(policy.subject_keys(db, group, 'group')) if group else set()
    candidates = [r for r in db.scalars(select(Resource).order_by(Resource.name, Resource.id)) if policy.can_manage_resource(actor, r)]
    candidate_ids = {r.id for r in candidates}
    keys = user_keys | group_keys
    bindings = [b for b in db.scalars(select(Binding).where(Binding.resource_id.in_(candidate_ids)))
                if (b.subject_type, b.subject_id) in keys] if candidate_ids else []
    rows = []
    for resource in candidates:
        user_sources = [b for b in bindings if b.resource_id == resource.id and (b.subject_type, b.subject_id) in user_keys]
        group_sources = [b for b in bindings if b.resource_id == resource.id and (b.subject_type, b.subject_id) in group_keys]
        owner = group or user
        explicit_admin = not group and user.role == 'super_admin' and any(b.subject_type == 'user' and b.subject_id == user.id for b in user_sources)
        reasons = policy.resource_access_reasons(resource, can_send=can_send, user_granted=bool(user_sources),
            group_granted=not group or bool(group_sources),
            in_scope=policy.in_resource_scope(resource, owner.org_id, owner.team_id) or explicit_admin)
        sources = [Source(side=side, subject_type=b.subject_type, subject_id=b.subject_id)
                   for side, matches in (('user', user_sources), ('group', group_sources)) for b in matches]
        rows.append(PreviewResource(id=resource.id, name=resource.name, kind=resource.kind, enabled=resource.enabled,
                                    effective=not reasons, reasons=reasons, sources=sources))
    return Preview(user_id=user.id, group_id=group_id, can_send=can_send, context_reasons=context_reasons, resources=rows)


@router.get('/api/bindings/preview', response_model=Preview)
def preview(user_id: str = Query(min_length=1, max_length=36), group_id: str | None = Query(None, min_length=1, max_length=36),
            actor=Depends(current_user), db=Depends(get_db, scope='function')):
    return evaluate(db, actor, user_id, group_id)
