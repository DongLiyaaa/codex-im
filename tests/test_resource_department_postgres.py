"""Department scope and encrypted header secrets for Skill / MCP resources through the real application functions."""
import secrets

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_im_postgres import database
from app import directory as d, im, im_discovery as discovery, main, policy, schemas, service
from app.models import Binding, Conversation, Group, Identity, Resource, Run, User

SKILL = {'content': '---\nname: "test"\ndescription: "test"\n---\nhello'}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('SESSION_SECRET', secrets.token_hex(32))
    monkeypatch.setattr(service, 'validate_mcp_url', lambda url: None)  # the address check resolves DNS


def person(db, name, role, org=None, team=None, active=True):
    user = User(email=f'{name}@example.invalid', name=name, role=role, org_id=org, team_id=team, active=active, password_hash='unused')
    db.add(user)
    db.flush()
    return user


def company(db):
    root = person(db, 'root', 'super_admin')
    org = d.create_organization(d.CreateName(name='公司'), root, db)['id']
    ops = d.create_department(d.CreateDepartment(name='运营部', org_id=org), root, db)['id']
    finance = d.create_department(d.CreateDepartment(name='财务部', org_id=org), root, db)['id']
    return root, org, ops, finance


def create(db, actor, **values):
    body = schemas.ResourceCreate(**{'name': 'r', 'kind': 'skill', 'config': SKILL, **values})
    return db.get(Resource, main.create_resource(body, actor, db)['id'])


def grant(db, subject_type, subject, resource):
    db.add(Binding(subject_type=subject_type, subject_id=subject.id, resource_id=resource.id))
    db.flush()


def conversation(db, owner, group=None):
    row = Conversation(title='t', owner_id=owner.id, group_id=group.id if group else None)
    db.add(row)
    db.flush()
    return row


def test_a_department_resource_reaches_only_members_of_that_department(database):
    with database.begin() as db:
        root, org, ops, finance = company(db)
        in_ops, in_finance = person(db, 'ops', 'member', org, ops), person(db, 'fin', 'member', org, finance)
        org_level = person(db, 'wide', 'member', org, None)
        created = main.create_resource(schemas.ResourceCreate(name='运营专用', kind='skill', org_id=org, team_id=ops, config=SKILL), root, db)
        assert created['org_id'] == org and created['team_id'] == ops
        resource = db.get(Resource, created['id'])
        for user in (in_ops, in_finance, org_level):
            grant(db, 'user', user, resource)
        assert [r.id for r in main.visible_resources(db, in_ops)] == [resource.id]
        assert main.visible_resources(db, in_finance) == [] and main.visible_resources(db, org_level) == []
        assert [r.id for r in policy.effective_resources(db, in_ops, conversation(db, in_ops))] == [resource.id]
        assert policy.effective_resources(db, in_finance, conversation(db, in_finance)) == []
        assert policy.effective_resources(db, org_level, conversation(db, org_level)) == []


def test_organization_and_global_resources_are_unchanged_by_departments(database):
    with database.begin() as db:
        root, org, ops, finance = company(db)
        in_ops = person(db, 'ops', 'member', org, ops)
        whole_org = create(db, root, name='全组织', org_id=org)
        everyone = create(db, root, name='全局')
        other_org = create(db, root, name='别的组织', org_id='other')
        for resource in (whole_org, everyone, other_org):
            grant(db, 'user', in_ops, resource)
        names = {r.name for r in policy.effective_resources(db, in_ops, conversation(db, in_ops))}
        assert names == {'全组织', '全局'}
        assert {r.name for r in main.visible_resources(db, in_ops)} == {'全组织', '全局'}


def test_group_chats_use_the_groups_department(database):
    with database.begin() as db:
        root, org, ops, finance = company(db)
        member = person(db, 'ops', 'member', org, ops)
        ops_group = Group(name='运营群', org_id=org, team_id=ops, member_ids=[member.id], provider='feishu', external_id='ops-chat')
        wide_group = Group(name='全公司群', org_id=org, team_id=None, member_ids=[member.id], provider='feishu', external_id='wide-chat')
        db.add_all([ops_group, wide_group])
        db.flush()
        department_only = create(db, root, name='运营专用', org_id=org, team_id=ops)
        whole_org = create(db, root, name='全组织', org_id=org)
        for resource in (department_only, whole_org):
            grant(db, 'user', member, resource)
            grant(db, 'group', ops_group, resource)
            grant(db, 'group', wide_group, resource)
        in_ops_group = {r.name for r in policy.effective_resources(db, member, conversation(db, member, ops_group))}
        in_wide_group = {r.name for r in policy.effective_resources(db, member, conversation(db, member, wide_group))}
        assert in_ops_group == {'运营专用', '全组织'}
        assert in_wide_group == {'全组织'}  # a group with no department cannot use a department's Skill / MCP


def test_who_may_create_and_grant_a_department_resource(database):
    with database.begin() as db:
        root, org, ops, finance = company(db)
        other_org = d.create_organization(d.CreateName(name='分公司'), root, db)['id']
        other_dept = d.create_department(d.CreateDepartment(name='别家部门', org_id=other_org), root, db)['id']
        admin = person(db, 'admin', 'org_admin', org)
        stranger = person(db, 'stranger', 'org_admin', other_org)
        lead = person(db, 'lead', 'team_lead', org, ops)
        assert create(db, admin, name='部门技能', org_id=org, team_id=ops).team_id == ops
        for actor in (stranger, lead):
            with pytest.raises(HTTPException) as denied:
                main.create_resource(schemas.ResourceCreate(name='x', kind='skill', org_id=org, team_id=ops, config=SKILL), actor, db)
            assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as mismatch:  # a department of another organization
            main.create_resource(schemas.ResourceCreate(name='x', kind='skill', org_id=org, team_id=other_dept, config=SKILL), root, db)
        assert mismatch.value.status_code == 403
        assert db.scalars(select(Resource).where(Resource.name == 'x')).first() is None
        # A department resource cannot be granted to someone from another department.
        outsider = person(db, 'fin', 'member', org, finance)
        resource = create(db, root, name='运营专用', org_id=org, team_id=ops)
        assert not policy.can_manage_binding(db, root, Binding(subject_type='user', subject_id=outsider.id, resource_id=resource.id))
        insider = person(db, 'ops', 'member', org, ops)
        assert policy.can_manage_binding(db, root, Binding(subject_type='user', subject_id=insider.id, resource_id=resource.id))


def test_header_secrets_are_encrypted_in_the_database_and_masked_in_the_listing(database):
    plain = {'Authorization': 'Bearer ' + secrets.token_hex(12), 'X-Api-Key': secrets.token_hex(12)}
    with database.begin() as db:
        root, org, ops, finance = company(db)
        body = schemas.ResourceCreate(name='库存', kind='mcp', config={'url': 'https://mcp.example.com/mcp', 'headers': plain})
        shown = main.create_resource(body, root, db)
        assert shown['config']['headers'] == {'Authorization': '***', 'X-Api-Key': '***'}
        resource_id = shown['id']
    with database.begin() as db:
        stored = db.get(Resource, resource_id).config
        assert set(stored['headers']) == set(plain)
        assert all(value.startswith('enc1:') for value in stored['headers'].values())
        assert not any(secret in str(stored) for secret in plain.values())
        root = db.scalars(select(User).where(User.role == 'super_admin')).one()
        listed = [service.resource_out(r, root) for r in main.visible_resources(db, root)]
        assert len(listed) == 1 and listed[0]['config']['headers'] == {'Authorization': '***', 'X-Api-Key': '***'}
        assert all(secret not in str(listed) for secret in plain.values())


def test_the_runner_receives_decrypted_headers_and_only_the_resources_in_scope(database):
    plain = {'Authorization': 'Bearer ' + secrets.token_hex(12), 'X-Api-Key': secrets.token_hex(12)}
    with database.begin() as db:
        root, org, ops, finance = company(db)
        identity = Identity(provider='feishu', external_user_id='root-sender', user_id=root.id)
        db.add(identity)
        db.flush()
        discovery.pin(db, 'identity', identity.id, discovery.scope('feishu'))
        discovery.record(db, 'feishu', discovery.scope('feishu'), 'root-sender', 'new-group', True, 'unknown_group')
        found = next(r for r in discovery.discovered_groups(root, db) if r['external_id'] == 'new-group')
        group_id = discovery.bind_group(found['id'], discovery.BindGroup(name='协作', org_id=org, member_ids=[root.id], confirm_members=True), root, db)['group_id']
        group = db.get(Group, group_id)
        connected = create(db, root, name='库存', kind='mcp', config={'url': 'https://mcp.example.com/mcp', 'headers': plain})
        department_only = create(db, root, name='运营专用', org_id=org, team_id=ops)
        for resource in (connected, department_only):
            grant(db, 'user', root, resource)
            grant(db, 'group', group, resource)
        assert im._enqueue(db, 'feishu', 'first', 'root-sender', 'new-group', 'hello', True) == {'ok': True}
        payload = service.build_payload(db, db.scalar(select(Run)))
        assert payload['mcps'] == [{'name': connected.id, 'url': 'https://mcp.example.com/mcp', 'headers': plain}]
        assert payload['skills'] == []  # the group has no department, so the department's Skill stays out
