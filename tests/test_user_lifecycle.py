"""Deactivation cuts every live path to a user's access; reactivation restores access and nothing else."""
import secrets
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from test_im_postgres import database
from test_setup import http
from app import im, main, schemas, security, user_lifecycle
from app.models import (Audit, IMDiscovery, LOCKED_PASSWORD, PlatformApproval, PlatformAuthJob, PlatformConnection,
                        Run, SessionToken, User, now)


def actor(role='super_admin', org=None, team=None, identifier='root'):
    return SimpleNamespace(id=identifier, role=role, active=True, org_id=org, team_id=team)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def audits(db, name):
    return [a for a in db.scalars(select(Audit)) if a.action == name]


def change(db, who, identifier, **body):
    target = user_lifecycle.manageable(db, who, identifier)
    user_lifecycle.apply(db, who, target, schemas.UserUpdate(**body))
    return target


def seeded(db):
    return db.scalar(select(User).where(User.org_id == 'org'))


def add_user(db, role='member', org='org', team='team', locked=False, name='同事'):
    user = User(email=f'{secrets.token_hex(6)}@example.invalid', name=name, role=role, org_id=org, team_id=team, active=True,
                password_hash=LOCKED_PASSWORD if locked else security.hash_password(secrets.token_urlsafe(18)))
    db.add(user)
    db.flush()
    return user


def give_access(db, user):
    """Everything a working user holds: web session, queued and running runs, platform authorization, an open approval."""
    from app.models import Conversation
    db.add(SessionToken(token_hash=secrets.token_hex(32), user_id=user.id, expires_at=now() + timedelta(hours=1)))
    assert im._enqueue(db, 'feishu', 'queued-run', 'sender', 'dm-one', 'hello', False) == {'ok': True}
    assert im._enqueue(db, 'feishu', 'running-run', 'sender', 'chat', 'hello', True) == {'ok': True}
    runs = list(db.scalars(select(Run).where(Run.user_id == user.id).order_by(Run.created_at)))
    runs[1].status = 'running'
    for provider, state in (('feishu', 'connected'), ('dingtalk', 'pending')):
        db.add(PlatformConnection(user_id=user.id, provider=provider, state=state, encrypted=secrets.token_hex(16),
                                  expires_at=now() + timedelta(hours=1), next_poll_at=now()))
        db.add(PlatformAuthJob(user_id=user.id, provider=provider, phase='poll', fingerprint=secrets.token_hex(8)))
    conversation = db.scalar(select(Conversation))
    db.add(PlatformApproval(code='ABC234', user_id=user.id, conversation_id=conversation.id, provider='feishu', operation='command',
                            digest=secrets.token_hex(8), summary='delete a document', payload={'args': ['x']},
                            expires_at=now() + timedelta(minutes=10)))
    db.flush()
    return runs


def test_deactivation_cuts_web_runs_platform_authorization_and_approvals(database):
    with database.begin() as db:
        user = seeded(db)
        queued, running = give_access(db, user)
    with database.begin() as db:
        change(db, actor(), user.id, active=False)
    with database() as db:
        assert db.get(User, user.id).active is False
        assert count(db, SessionToken) == 0
        assert {r.id: (r.status, r.error) for r in db.scalars(select(Run))} == {
            queued.id: ('cancelled', 'Cancelled: user deactivated'), running.id: ('cancelled', 'Cancelled: user deactivated')}
        for row in db.scalars(select(PlatformConnection)):
            assert (row.state, row.encrypted, row.expires_at, row.next_poll_at) == ('disconnected', '', None, None)
        assert {j.phase: j.notification for j in db.scalars(select(PlatformAuthJob))} == {'done': 'cancelled'}
        approval = db.scalar(select(PlatformApproval))
        assert (approval.state, approval.payload) == ('denied', {})
        detail = audits(db, 'user.deactivate')[0].details
        assert (detail['sessions_revoked'], detail['runs_cancelled'], detail['approvals_closed']) == (1, 2, 1)
        assert sorted(detail['connections_cleared']) == ['dingtalk', 'feishu']
        assert len(audits(db, 'run.cancelled')) == 2


def test_a_deactivated_sender_is_refused_and_not_treated_as_a_new_person(database):
    with database.begin() as db:
        user = seeded(db)
        change(db, actor(), user.id, active=False)
    with database.begin() as db:
        assert im._enqueue(db, 'feishu', 'after-1', 'sender', 'dm-two', 'hello', False)['pending']
        assert im._enqueue(db, 'feishu', 'after-2', 'sender', 'chat', 'hello', True)['pending']
    with database() as db:
        assert count(db, Run) == 0
        assert {r.reason for r in db.scalars(select(IMDiscovery))} == {'inactive_user'}


def test_reactivation_restores_access_but_never_platform_authorization(database):
    with database.begin() as db:
        user = seeded(db)
        give_access(db, user)
        change(db, actor(), user.id, active=False)
    with database.begin() as db:
        assert change(db, actor(), user.id, active=True).active is True
    with database.begin() as db:
        assert im._enqueue(db, 'feishu', 'back-again', 'sender', 'dm-three', 'hello', False) == {'ok': True}
    with database() as db:
        assert all((c.state, c.encrypted) == ('disconnected', '') for c in db.scalars(select(PlatformConnection)))
        assert count(db, SessionToken) == 0 and len(audits(db, 'user.activate')) == 1


def test_repeating_a_change_is_harmless_and_leaves_no_second_trail(database):
    with database.begin() as db:
        user = seeded(db)
        give_access(db, user)
    for _ in range(3):
        with database.begin() as db:
            change(db, actor(), user.id, active=False)
    with database() as db:
        assert len(audits(db, 'user.deactivate')) == 1 and len(audits(db, 'run.cancelled')) == 2
    for _ in range(2):
        with database.begin() as db:
            change(db, actor(), user.id, active=True)
    with database() as db:
        assert len(audits(db, 'user.activate')) == 1


@pytest.mark.parametrize('who,target_role,target_org,target_team,allowed', [
    (actor(), 'member', 'org', 'team', True),
    (actor(), 'org_admin', 'org', None, True),
    (actor('org_admin', 'org'), 'member', 'org', 'team', True),
    (actor('org_admin', 'org'), 'team_lead', 'org', 'team', True),
    (actor('org_admin', 'org'), 'member', 'other', 'team', False),
    (actor('org_admin', 'org'), 'org_admin', 'org', None, False),
    (actor('team_lead', 'org', 'team'), 'member', 'org', 'team', True),
    (actor('team_lead', 'org', 'team'), 'member', 'org', 'elsewhere', False),
    (actor('team_lead', 'org', 'team'), 'team_lead', 'org', 'team', False),
    (actor('member', 'org', 'team'), 'member', 'org', 'team', False),
    (actor(), 'super_admin', None, None, False),
])
def test_only_people_who_already_manage_a_user_can_change_it(database, who, target_role, target_org, target_team, allowed):
    with database.begin() as db:
        target = add_user(db, target_role, target_org, target_team).id
    outcome = []
    for body in ({'active': False}, {'name': '新名字'}):
        try:
            with database.begin() as db:
                change(db, who, target, **body)
            outcome.append(True)
        except HTTPException as exc:
            assert exc.status_code == 403
            outcome.append(False)
    assert outcome == [allowed, allowed]
    with database() as db:
        user = db.get(User, target)
        assert (user.active, user.name) == ((not allowed, '新名字') if allowed else (True, '同事'))


def test_nobody_can_change_themselves_or_a_missing_user(database):
    with database.begin() as db:
        admin = User(email='boss@example.invalid', name='老板', role='super_admin', active=True, password_hash=LOCKED_PASSWORD)
        db.add(admin)
        db.flush()
        admin_id = admin.id
    for body in ({'active': False}, {'name': '改名'}):
        with pytest.raises(HTTPException) as caught:
            with database.begin() as db:
                change(db, actor(identifier=admin_id), admin_id, **body)
        assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            change(db, actor(), 'missing', active=False)
    assert caught.value.status_code == 404
    with database() as db:
        assert db.get(User, admin_id).active is True and db.get(User, admin_id).name == '老板'


def test_renaming_cleans_the_name_and_keeps_names_out_of_the_audit_trail(database):
    with database.begin() as db:
        target = add_user(db, locked=True).id
    with database.begin() as db:
        change(db, actor(), target, name='  王小明\u200b  ')
        change(db, actor(), target, name='王小明')
    with database() as db:
        assert db.get(User, target).name == '王小明'
        assert len(audits(db, 'user.rename')) == 1
        assert '王小明' not in str([a.details for a in db.scalars(select(Audit))])
    with pytest.raises(HTTPException) as caught:  # Invisible characters survive request validation but not cleaning.
        with database.begin() as db:
            change(db, actor(), target, name='\u200b')
    assert caught.value.status_code == 400
    with pytest.raises(ValidationError):  # Plain whitespace never reaches the handler.
        schemas.UserUpdate(name='\n\t')


@pytest.mark.parametrize('body', [{}, {'name': None}, {'name': ''}, {'name': 'x' * 201}, {'role': 'super_admin'},
                                  {'active': False, 'email': 'x@example.invalid'}, {'active': 'maybe'}, {'org_id': 'other'}])
def test_update_requests_accept_only_a_name_or_the_active_flag(body):
    with pytest.raises(ValidationError):
        schemas.UserUpdate(**body)


def test_an_rolled_back_deactivation_leaves_everything_as_it_was(database):
    with database.begin() as db:
        user = seeded(db)
        give_access(db, user)
    with pytest.raises(RuntimeError):
        with database.begin() as db:
            change(db, actor(), user.id, active=False)
            raise RuntimeError('simulated failure after the change')
    with database() as db:
        assert db.get(User, user.id).active is True and count(db, SessionToken) == 1
        assert {r.status for r in db.scalars(select(Run))} == {'queued', 'running'}
        assert {c.state for c in db.scalars(select(PlatformConnection))} == {'connected', 'pending'}
        assert db.scalar(select(PlatformApproval)).state == 'pending'


def test_locked_members_stay_unable_to_log_in_through_a_deactivation_cycle(database):
    with database.begin() as db:
        target = add_user(db, locked=True)
        email = target.email
        target_id = target.id
    for active in (False, True):
        with database.begin() as db:
            change(db, actor(), target_id, active=active)
    with database() as db:
        assert db.get(User, target_id).password_hash == LOCKED_PASSWORD


def test_http_administrator_deactivates_a_web_user_whose_session_and_login_stop_working(database):
    admin_password, member_password = secrets.token_urlsafe(20), secrets.token_urlsafe(20)
    with database.begin() as db:
        db.add(User(email='root@example.invalid', name='管理员', role='super_admin', active=True,
                    password_hash=security.hash_password(admin_password)))
        member = add_user(db)
        member.email, member.password_hash = 'staff@example.invalid', security.hash_password(member_password)
        member_id = member.id
    boss, staff = http(database), None
    try:
        assert boss.post('/api/auth/login', json={'email': 'root@example.invalid', 'password': admin_password}).status_code == 200
        from fastapi.testclient import TestClient
        staff = TestClient(main.app)
        assert staff.post('/api/auth/login', json={'email': 'staff@example.invalid', 'password': member_password}).status_code == 200
        assert staff.get('/api/auth/me').status_code == 200
        listed = {u['id']: u for u in boss.get('/api/users').json()}
        assert listed[member_id]['can_manage'] is True and listed[member_id]['active'] is True
        assert [u['can_manage'] for u in listed.values() if u['role'] == 'super_admin'] == [False]
        response = boss.patch(f'/api/users/{member_id}', json={'active': False})
        assert response.status_code == 200 and response.json()['active'] is False and response.json()['can_manage'] is True
        assert staff.get('/api/auth/me').status_code == 401
        assert staff.post('/api/auth/login', json={'email': 'staff@example.invalid', 'password': member_password}).status_code == 401
        assert boss.patch(f'/api/users/{member_id}', json={'active': True}).json()['active'] is True
        assert staff.post('/api/auth/login', json={'email': 'staff@example.invalid', 'password': member_password}).status_code == 200
        assert boss.patch(f'/api/users/{member_id}', json={'name': '王小明'}).json()['name'] == '王小明'
        for bad in ({}, {'role': 'super_admin'}, {'name': ''}):
            assert boss.patch(f'/api/users/{member_id}', json=bad).status_code == 422
        assert boss.patch('/api/users/missing', json={'active': False}).status_code == 404
        me = boss.get('/api/auth/me').json()
        assert boss.patch(f"/api/users/{me['id']}", json={'active': False}).status_code == 403
    finally:
        boss.close()
        if staff:
            staff.close()
        main.app.dependency_overrides.clear()
