"""Directory and global-admin group participation in isolated PostgreSQL."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from test_im_postgres import database
from app import directory as d, im_discovery as discovery, im, policy, service, main, schemas
from app.models import User, Group, Identity, IMDiscovery, Resource, Binding, Run, Conversation


def test_directory_permissions_legacy_and_conflict(database):
    with database.begin() as db:
        member = db.scalar(select(User))
        root = User(id='root', role='super_admin', active=True)
        org = d.create_organization(d.CreateName(name='运营公司'), root, db)
        dept = d.create_department(d.CreateDepartment(name='广告部', org_id=org['id']), root, db)
        assert d.catalog(db, member)['organizations'] == [{'id':'org','name':'org','legacy':True}]
        assert d.catalog(db, member)['departments'][0]['id'] == 'team'
        for role in ['org_admin', 'team_lead', 'member']:
            actor = User(id=role, role=role, org_id='org', team_id='team', active=True)
            with pytest.raises(HTTPException): d.create_organization(d.CreateName(name='禁止'), actor, db)
            with pytest.raises(HTTPException): d.create_department(d.CreateDepartment(name='禁止',org_id=org['id']), actor, db)
            if role != 'org_admin':
                with pytest.raises(HTTPException): d.create_department(d.CreateDepartment(name='禁止',org_id='org'), actor, db)
        admin = User(id='admin',role='org_admin',org_id='org',active=True)
        d.create_department(d.CreateDepartment(name='本组织部门',org_id='org'),admin,db)
        with pytest.raises(HTTPException): d.validate_scope(db,'org',dept['id'])
        d.validate_scope(db,'org','team')
        created = main.create_user(schemas.UserCreate(name='组织管理员',email='admin@example.invalid',password='test-password-123',role='org_admin',org_id=org['id']),root,db)
        assert created.org_id == org['id'] and created.team_id is None
    with pytest.raises(IntegrityError):
        with database.begin() as db: d.create_organization(d.CreateName(name='运营公司'),root,db)
    with pytest.raises(IntegrityError):
        with database.begin() as db: d.create_department(d.CreateDepartment(name='广告部',org_id=org['id']),root,db)


def test_global_admin_binding_enqueue_resources_and_revocation(database):
    with database.begin() as db:
        root=User(email='root@example.invalid',name='Super Admin',password_hash='unused',role='super_admin',active=True)
        db.add(root);db.flush(); uid=root.id
        identity=Identity(provider='feishu',external_user_id='root-sender',user_id=uid)
        db.add(identity);db.flush();discovery.pin(db,'identity',identity.id,discovery.scope('feishu'))
        org=d.create_organization(d.CreateName(name='新组织'),root,db)
        discovery.record(db,'feishu',discovery.scope('feishu'),'root-sender','new-group',True,'unknown_group')
        found=next(r for r in discovery.discovered_groups(root,db) if r['external_id']=='new-group')
        gid=discovery.bind_group(found['id'],discovery.BindGroup(name='协作',org_id=org['id'],member_ids=[uid],confirm_members=True),root,db)['group_id']
        for name,scope in [('own',org['id']),('foreign','other'),('global',None)]:
            resource=Resource(name=name,org_id=scope,kind='skill',enabled=True,config={'content':'---\nname: test\ndescription: test\n---\nhello'})
            db.add(resource);db.flush()
            binding=Binding(subject_type='user',subject_id=uid,resource_id=resource.id)
            assert policy.can_manage_binding(db,root,binding)
            db.add(binding);db.add(Binding(subject_type='group',subject_id=gid,resource_id=resource.id))
        assert im._enqueue(db,'feishu','first','root-sender','new-group','hello',True)=={'ok':True}
        run=db.scalar(select(Run)); rid=run.id
        payload=service.build_payload(db,run)
        assert len(payload['skills'])==2
        conv=db.get(Conversation,run.conversation_id)
        assert {r.name for r in policy.effective_resources(db,root,conv)}=={'own','global'}
        assert root.org_id is None and root.role=='super_admin'
        private=Conversation(title='private',owner_id=uid);db.add(private);db.flush()
        assert {r.name for r in policy.effective_resources(db,root,private)}=={'global'}
    with database.begin() as db:
        db.get(Group,gid).member_ids=[]
    with database.begin() as db:
        with pytest.raises(HTTPException): service.build_payload(db,db.get(Run,rid))
        assert im._enqueue(db,'feishu','second','root-sender','new-group','hello',True)['pending']


def test_cross_department_and_tenant_boundaries(database):
    with database.begin() as db:
        root=User(id='root',role='super_admin',active=True)
        first=db.scalar(select(User))
        second=User(email='second@example.invalid',name='其他部门',password_hash='unused',role='member',org_id='org',team_id='second',active=True)
        db.add(second);db.flush()
        group=main.create_group(schemas.GroupCreate(name='跨部门',org_id='org',member_ids=[first.id,second.id]),root,db)
        assert policy.is_group_member(first,group) and policy.is_group_member(second,group)
        group.team_id='team'
        assert not policy.is_group_member(second,group)
        second.org_id='foreign';group.team_id=None
        assert not policy.is_group_member(second,group)
        with pytest.raises(HTTPException): main.create_group(schemas.GroupCreate(name='混组织',org_id='org',member_ids=[first.id,second.id]),root,db)
