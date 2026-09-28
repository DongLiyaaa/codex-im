"""Isolated real PostgreSQL coverage for management pagination and group sync."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from test_im_postgres import database
from app import main, im_discovery as d
from app.models import User, Audit, Group, Binding, IMDiscovery, now


def actor(role='super_admin', **kwargs):
    return SimpleNamespace(id='root', role=role, active=True, org_id='org', team_id='team', **kwargs)


@pytest.mark.parametrize('size', [50, 100])
def test_audit_sql_pages(database, size):
    with database.begin() as db:
        uid = db.scalar(select(User.id)); stamp = now()
        db.add_all([Audit(id=f'{i:036d}', actor_id=uid, action='test', created_at=stamp) for i in range(605)])
        db.add(Audit(id='foreign', actor_id='foreign', action='test', created_at=stamp))
    with database() as db:
        root = main.audit_page(1, size, actor(), db)
        assert root['total'] == 606 and len(root['items']) == size
        first = main.audit_page(1, size, actor('org_admin'), db)
        second = main.audit_page(2, size, actor('org_admin'), db)
        assert first['total'] == 605 and len(second['items']) == size
        assert first['items'][0]['id'] == f'{604:036d}'
        assert not {r['id'] for r in first['items']} & {r['id'] for r in second['items']}
        last = main.audit_page(2147483647, size, actor('team_lead'), db)
        assert last['page'] == last['pages'] and len(last['items']) == 5
        a = actor('org_admin'); a.org_id = 'other'
        empty = main.audit_page(2, size, a, db)
        assert empty['total'] == 0 and empty['page'] == empty['pages'] == 1
        with pytest.raises(HTTPException): main.audit_page(1, size, actor('member'), db)
        with pytest.raises(HTTPException): main.audit_page(1, 51, actor(), db)


def test_group_sync_scope_dedup_bind(database, monkeypatch):
    with database.begin() as db:
        scope = d.scope('feishu')
        for sender, chat, group, app in [('sender','fresh',True,scope), ('other','fresh',True,scope), ('sender','private',False,scope), ('sender','chat',True,scope), ('sender','old',True,'old')]:
            d.record(db,'feishu',app,sender,chat,group,'unknown_group')
    with database.begin() as db:
        rows = d.discovered_groups(actor(), db)
        assert len(rows) == 1 and rows[0]['external_id'] == 'fresh'
        assert rows[0]['name'] is None and len(rows[0]['members']) == 1
        row = rows[0]; uid = row['members'][0]['id']
        result = d.bind_group(row['id'],d.BindGroup(name='fresh',org_id='org',team_id='team',member_ids=[uid,uid],confirm_members=True),actor(),db)
        assert db.get(Group,result['group_id']).member_ids == [uid]
        assert d.discovered_groups(actor(), db) == []
        assert db.scalar(select(func.count()).select_from(Binding)) == 0
    monkeypatch.setenv('FEISHU_APP_ID','rotated')
    with database() as db:
        assert d.discovered_groups(actor(),db) == []
        with pytest.raises(HTTPException) as exc: d.bind_group(row['id'],d.BindGroup(confirm_members=True),actor(),db)
        assert exc.value.status_code == 409


@pytest.mark.parametrize('role',['member','org_admin','team_lead'])
def test_discovered_groups_global_access(database,role):
    with database() as db:
        with pytest.raises(HTTPException): d.discovered_groups(actor(role),db)
        with pytest.raises(HTTPException): d.bind_group('missing',d.BindGroup(),actor(role),db)


@pytest.mark.parametrize('failure',['private','unconfirmed','unbound','wrong_scope','existing'])
def test_group_bind_validation(database,failure):
    with database.begin() as db:
        d.record(db,'feishu',d.scope('feishu'),'sender','fresh',failure!='private','unknown_group')
        rid=db.scalar(select(IMDiscovery.id)); uid=db.scalar(select(User.id))
        body=d.BindGroup(name='fresh',org_id='wrong' if failure=='wrong_scope' else 'org',team_id='team',member_ids=['unbound' if failure=='unbound' else uid],confirm_members=failure!='unconfirmed')
        if failure=='existing':
            group=db.scalar(select(Group)); group.external_id=None; group.provider='web'
            body=d.BindGroup(group_id=group.id,confirm_members=True)
    if failure=='existing':
        with database.begin() as db:
            d.bind_group(rid,body,actor(),db)
            assert db.scalar(select(Group)).external_id=='fresh'
    else:
        with pytest.raises(HTTPException):
            with database.begin() as db: d.bind_group(rid,body,actor(),db)
        with database() as db: assert db.scalar(select(func.count()).select_from(Audit))==0
