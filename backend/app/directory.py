"""Named internal directory; legacy string references remain unchanged."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select, func
from .models import now
from .db import get_db
from .models import Organization, Department, User, Group, Resource
from .security import current_user
from . import policy, schemas, service

router = APIRouter(prefix='/api/directory', tags=['directory'])


def catalog(db, actor):
    policy.require(actor.active)
    global_access = actor.role == 'super_admin'
    orgs, teams = {}, {}
    for model in (Organization, User, Group, Resource):
        column = model.id if model is Organization else model.org_id
        query = select(model).where(column.is_not(None))
        if model is Organization:
            query = query.where(Organization.archived_at.is_(None))
        if not global_access:
            query = query.where(column == actor.org_id) if actor.org_id else query.where(False)
        for row in db.scalars(query):
            identifier = row.id if model is Organization else row.org_id
            if model is Organization:
                orgs[identifier] = {'id': identifier, 'name': row.name, 'legacy': False}
            else:
                orgs.setdefault(identifier, {'id': identifier, 'name': identifier, 'legacy': True})
    for model in (Department, User, Group):
        column = model.id if model is Department else model.team_id
        query = select(model).where(column.is_not(None), model.org_id.is_not(None))
        if model is Department:
            query = query.where(Department.archived_at.is_(None))
        if not global_access:
            query = query.where(model.org_id == actor.org_id) if actor.org_id else query.where(False)
            if actor.role != 'org_admin':
                query = query.where(column == actor.team_id) if actor.team_id else query.where(False)
        for row in db.scalars(query):
            identifier = row.id if model is Department else row.team_id
            key = (row.org_id, identifier)
            if model is Department:
                teams[key] = {'id': identifier, 'org_id': row.org_id, 'name': row.name, 'legacy': False}
            else:
                teams.setdefault(key, {'id': identifier, 'org_id': row.org_id, 'name': identifier, 'legacy': True})
    return {'organizations': sorted(orgs.values(), key=lambda r: (r['name'], r['id'])),
            'departments': sorted(teams.values(), key=lambda r: (r['name'], r['id'])),
            'can_create_org': global_access,
            'can_create_department': global_access or (actor.role == 'org_admin' and bool(actor.org_id))}


@router.get('')
def directory(actor=Depends(current_user), db=Depends(get_db, scope='function')):
    return catalog(db, actor)


class CreateName(schemas.Input):
    name: str = Field(min_length=1, max_length=200)


class CreateDepartment(CreateName):
    org_id: str = Field(min_length=1, max_length=100)


@router.post('/organizations', status_code=201)
def create_organization(body: CreateName, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    policy.require(actor.active and actor.role == 'super_admin')
    row = Organization(name=body.name)
    db.add(row)
    db.flush()
    service.audit(db, actor, 'organization.create', row.id)
    return {'id': row.id, 'name': row.name, 'legacy': False}


@router.post('/departments', status_code=201)
def create_department(body: CreateDepartment, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    policy.require(actor.active and (actor.role == 'super_admin' or
        (actor.role == 'org_admin' and actor.org_id == body.org_id)))
    validate_scope(db, body.org_id, None)
    policy.require(any(o['id'] == body.org_id for o in catalog(db, actor)['organizations']), 'Unknown organization')
    row = Department(name=body.name, org_id=body.org_id)
    db.add(row)
    db.flush()
    service.audit(db, actor, 'department.create', row.id, {'org_id': row.org_id})
    return {'id': row.id, 'org_id': row.org_id, 'name': row.name, 'legacy': False}


def scope_lock(db):
    # All directory-reference writers take this transaction lock after IM config
    # locks and before group/conversation row locks. Tombstones forbid ID reuse.
    db.execute(select(func.pg_advisory_xact_lock(71905)))


def validate_scope(db, org_id, team_id):
    scope_lock(db)
    organization = db.get(Organization, org_id, populate_existing=True) if org_id else None
    if organization and organization.archived_at is not None:
        raise HTTPException(409, '组织已删除，不能继续使用该组织 ID。')
    if team_id:
        department = db.get(Department, team_id, populate_existing=True)
        if department and department.archived_at is not None:
            raise HTTPException(409, '部门已删除，不能继续使用该部门 ID。')
        if department and department.org_id != org_id:
            raise HTTPException(403, 'Department belongs to another organization')


def manageable(actor, row, deleting=False):
    return actor.active and (actor.role == 'super_admin' or (
        actor.role == 'org_admin' and
        ((isinstance(row, Department) and actor.org_id == row.org_id) or
         (isinstance(row, Organization) and not deleting and actor.org_id == row.id))))


@router.get('/management')
def management(actor=Depends(current_user), db=Depends(get_db, scope='function')):
    policy.require(actor.active and actor.role in ('super_admin', 'org_admin'))
    result = catalog(db, actor)
    for kind, model in [('organizations', Organization), ('departments', Department)]:
        for item in result[kind]:
            row = db.get(model, item['id']) if not item['legacy'] else None
            item['can_edit'] = bool(row and manageable(actor, row))
            item['can_delete'] = bool(row and manageable(actor, row, True))
    return result


def directory_row(db, actor, kind, identifier, deleting=False):
    scope_lock(db)
    model = {'organizations': Organization, 'departments': Department}.get(kind)
    if model is None:
        raise HTTPException(404, 'Not found')
    row = db.get(model, identifier)
    if not row or row.archived_at is not None:
        raise HTTPException(404, 'Not found')
    policy.require(manageable(actor, row, deleting))
    return row


@router.patch('/{kind}/{identifier}')
def rename(kind: str, identifier: str, body: CreateName, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    row = directory_row(db, actor, kind, identifier)
    old_name = row.name
    row.name = body.name
    db.flush()
    service.audit(db, actor, 'directory.rename', identifier, {'kind': kind, 'previous_name': old_name, 'name': row.name})
    return {'id': row.id, 'name': row.name}


@router.delete('/{kind}/{identifier}')
def remove(kind: str, identifier: str, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    row = directory_row(db, actor, kind, identifier, True)
    references = [(User, User.team_id), (Group, Group.team_id)] if isinstance(row, Department) else [
        (Department, Department.org_id), (User, User.org_id), (Group, Group.org_id), (Resource, Resource.org_id)]
    labels = {Department: '部门（含历史记录）', User: '用户（含停用用户）', Group: '群组（含已归档群组）', Resource: '资源'}
    for model, column in references:
        query = select(column).where(column == identifier)
        if db.scalar(query.limit(1)) is not None:
            raise HTTPException(409, f'无法删除：仍被{labels[model]}引用。请先处理关联；不会级联删除。')
    row.archived_at = now()
    service.audit(db, actor, 'directory.archive', identifier, {'kind': kind, 'name': row.name})
    return {'ok': True}
