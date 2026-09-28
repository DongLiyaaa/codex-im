"""Directory lifecycle and group revocation using isolated PostgreSQL only."""
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from fastapi import HTTPException
from sqlalchemy import select
from test_im_postgres import database
from app import main, directory as d, schemas, policy, service, im, im_discovery as discovery
from app.models import User, Group, Organization, Department, Conversation, Run, IMEvent, IMReaction, Binding, now


def root():
    return User(id='root', role='super_admin', active=True)


def test_directory_rename_delete_tombstone(database):
    with database.begin() as db:
        org=d.create_organization(d.CreateName(name='公司'),root(),db)
        dept=d.create_department(d.CreateDepartment(name='部门',org_id=org['id']),root(),db)
        d.rename('organizations',org['id'],d.CreateName(name='新公司'),root(),db)
        assert db.get(Organization,org['id']).name=='新公司'
        with pytest.raises(HTTPException) as e:d.remove('organizations',org['id'],root(),db)
        assert e.value.status_code==409
        d.remove('departments',dept['id'],root(),db)
        with pytest.raises(HTTPException):d.validate_scope(db,org['id'],dept['id'])
        empty=d.create_organization(d.CreateName(name='空公司'),root(),db)
        d.remove('organizations',empty['id'],root(),db)
        with pytest.raises(HTTPException):d.validate_scope(db,empty['id'],None)
        assert empty['id'] not in [o['id'] for o in d.catalog(db,root())['organizations']]


@pytest.mark.parametrize('role',['member','team_lead','org_admin'])
def test_directory_cross_tenant(database,role):
    with database.begin() as db:
        org=d.create_organization(d.CreateName(name='公司'),root(),db)
        actor=User(id='a',role=role,org_id='foreign',active=True)
        with pytest.raises(HTTPException):d.rename('organizations',org['id'],d.CreateName(name='越权'),actor,db)
        with pytest.raises(HTTPException):d.remove('organizations',org['id'],actor,db)
        actor.role='org_admin';actor.org_id=org['id']
        d.rename('organizations',org['id'],d.CreateName(name='自己的公司'),actor,db)
        with pytest.raises(HTTPException):d.remove('organizations',org['id'],actor,db)


@pytest.mark.parametrize('source',['user','group'])
def test_directory_historical_reference(database,source):
    with database.begin() as db:
        db.add(Organization(id='org',name='公司'));db.add(Department(id='team',org_id='org',name='部门'))
        if source=='user':db.scalar(select(User)).active=False
        else:db.scalar(select(Group)).archived_at=now()
        with pytest.raises(HTTPException) as e:d.remove('departments','team',root(),db)
        assert e.value.status_code==409


def test_group_revocation_and_archive_all_paths(database):
    with database.begin() as db:
        user=db.scalar(select(User));group=db.scalar(select(Group));gid=group.id
        other=User(name='另一个',email='other@test.invalid',password_hash='unused',role='member',org_id='org',team_id='team',active=True)
        db.add(other);db.flush()
        conv=Conversation(owner_id=user.id,group_id=gid,title='测试');db.add(conv);db.flush();cid=conv.id
        main.edit_group(gid,schemas.GroupUpdate(name='新群名',member_ids=[other.id]),root(),db)
        assert not policy.can_read_conversation(db,user,conv)
        main.delete_group(gid,root(),db)
        assert not main.groups(root(),db)
        assert not policy.can_manage_group(db,root(),group)
        for call in [lambda: main.messages(cid,other,db),lambda:main.conversation_state(cid,other,db),lambda:main.capabilities(cid,other,db),lambda:service.enqueue_message(db,other,conv,'hello'),lambda:main.create_conversation(schemas.ConversationCreate(title='x',group_id=gid),other,db)]:
            with pytest.raises(HTTPException):call()
        assert db.get(Group,gid).external_id=='chat'
        assert im._enqueue(db,'feishu','old-denied','sender','chat','test',True)['pending']
        found=discovery.discovered_groups(root(),db)[0]
        new=discovery.bind_group(found['id'],discovery.BindGroup(name='重新授权',org_id='org',team_id='team',member_ids=[user.id],confirm_members=True),root(),db)['group_id']
        assert new!=gid
        assert not list(db.scalars(select(Binding).where(Binding.subject_id==new)))
        assert im._enqueue(db,'feishu','old-denied','sender','chat','test',True)['duplicate']
        assert im._enqueue(db,'feishu','fresh','sender','chat','test',True)=={'ok':True}


@pytest.mark.parametrize('pending',['run','delivery','reaction'])
def test_group_busy_protection(database,pending):
    with database.begin() as db:
        im._enqueue(db,'feishu','event','sender','chat','hello',True)
        group=db.scalar(select(Group));run=db.scalar(select(Run));event=db.scalar(select(IMEvent));reaction=db.scalar(select(IMReaction))
        if pending!='run':run.status='succeeded'
        if pending!='reaction':reaction.state='cleared'
        if pending=='reaction':event.delivery_error='FAILED'
        for call in [lambda:main.delete_group(group.id,root(),db),lambda:main.edit_group(group.id,schemas.GroupUpdate(name='修改',member_ids=group.member_ids),root(),db)]:
            with pytest.raises(HTTPException) as e:call()
            assert e.value.status_code==409


def test_directory_delete_serializes_reference_creation(database):
    with database.begin() as db:org=d.create_organization(d.CreateName(name='并发公司'),root(),db)
    acquired=Event();release=Event()
    def remove():
        with database.begin() as db:
            d.remove('organizations',org['id'],root(),db);acquired.set();assert release.wait(5)
    def create():
        with database.begin() as db:
            main.create_user(schemas.UserCreate(name='用户',email='race@test.invalid',password='test-password-123',role='org_admin',org_id=org['id']),root(),db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion=pool.submit(remove);assert acquired.wait(5)
        creation=pool.submit(create);release.set();deletion.result(5)
        with pytest.raises(HTTPException):creation.result(5)


@pytest.mark.parametrize('role',['member','org_admin','team_lead'])
def test_group_management_requires_existing_member_scope(database,role):
    with database.begin() as db:
        group=db.scalar(select(Group));user=db.scalar(select(User))
        actor=User(id='foreign',role=role,org_id='other',team_id='other',active=True)
        with pytest.raises(HTTPException):main.edit_group(group.id,schemas.GroupUpdate(name='越权',member_ids=[user.id]),actor,db)
        with pytest.raises(HTTPException):main.delete_group(group.id,actor,db)
        if role=='member':
            with pytest.raises(HTTPException):main.delete_group(group.id,user,db)


def test_group_rejects_out_of_scope_members_and_run_access(database):
    with database.begin() as db:
        user=db.scalar(select(User));group=db.scalar(select(Group))
        foreign=User(name='foreign',email='f@test',password_hash='unused',role='member',org_id='foreign',team_id='team',active=True)
        db.add(foreign);db.flush()
        with pytest.raises(HTTPException):main.edit_group(group.id,schemas.GroupUpdate(name='x',member_ids=[foreign.id]),root(),db)
        im._enqueue(db,'feishu','event','sender','chat','hello',True)
        run=db.scalar(select(Run));run.status='succeeded'
        db.scalar(select(IMEvent)).delivery_error='FAILED';db.scalar(select(IMReaction)).state='cleared'
        main.delete_group(group.id,root(),db)
        with pytest.raises(HTTPException):main.run_status(run.id,user,db)
        with pytest.raises(HTTPException):service.build_payload(db,run)


def test_rename_conflict_preserves_id(database):
    from sqlalchemy.exc import IntegrityError
    with database.begin() as db:
        a=d.create_organization(d.CreateName(name='A'),root(),db)
        d.create_organization(d.CreateName(name='B'),root(),db)
    with pytest.raises(IntegrityError):
        with database.begin() as db:d.rename('organizations',a['id'],d.CreateName(name='B'),root(),db)
    with database() as db:assert db.get(Organization,a['id']).name=='A'


def test_group_archive_serializes_enqueue(database):
    with database.begin() as db:
        user=db.scalar(select(User));group=db.scalar(select(Group));gid=group.id;uid=user.id
        conv=Conversation(owner_id=uid,group_id=gid,title='race');db.add(conv);db.flush();cid=conv.id
    acquired=Event();release=Event()
    def archive():
        with database.begin() as db:
            main.delete_group(gid,root(),db);acquired.set();assert release.wait(5)
    def enqueue():
        with database.begin() as db:service.enqueue_message(db,db.get(User,uid),db.get(Conversation,cid),'hello')
    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion=pool.submit(archive);assert acquired.wait(5)
        send=pool.submit(enqueue);release.set();deletion.result(5)
        with pytest.raises(HTTPException):send.result(5)
