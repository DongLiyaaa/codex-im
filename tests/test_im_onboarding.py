"""Administrator-only onboarding: a discovered IM sender becomes an IM-only member, with no Agent Hub account."""
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from test_im_postgres import database
from test_setup import http
from app import im, im_discovery as d, main, security
from app.models import (Audit, Conversation, Department, IMDiscovery, Identity, LOCKED_PASSWORD, Organization,
                        Run, User, now)

PROVIDER = 'feishu'


def root(role='super_admin'):
    return SimpleNamespace(id='root', role=role, active=True, org_id=None, team_id=None)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def pending(db, sender='newbie', chat='dm', group=False, nickname='小王'):
    d.record(db, PROVIDER, d.scope(PROVIDER), sender, chat, group, 'unknown_sender', nickname)
    db.flush()
    return db.scalar(select(IMDiscovery).where(IMDiscovery.sender_id == sender, IMDiscovery.chat_id == chat))


def member(**overrides):
    return d.NewMember(**({'name': '小王', 'org_id': 'org', 'team_id': 'team'} | overrides))


def onboard(db, row, **overrides):
    return d.approve(row.id, d.Approve(new_user=member(**overrides)), root(), db)


def nothing_created(database):
    """The fixture seeds one member and one identity; a rejected onboarding must leave exactly those."""
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (1, 1)
        assert not [a for a in db.scalars(select(Audit)) if a.action in ('user.create', 'im.discovery.approve')]


def test_onboarding_creates_an_im_only_member_who_can_use_the_bot_after_resending(database):
    with database.begin() as db:
        row = pending(db)
        result = onboard(db, row, name='  小王  ')
    assert result['created_user'] and result['resend_required']
    with database() as db:
        created = db.get(User, result['user_id'])
        assert (created.name, created.role, created.org_id, created.team_id, created.active) == ('小王', 'member', 'org', 'team', True)
        assert created.password_hash == LOCKED_PASSWORD and not created.login_enabled
        assert created.email.endswith('@im.invalid') and created.email.startswith('im-')
        identity = db.scalar(select(Identity).where(Identity.external_user_id == 'newbie'))
        assert identity.user_id == created.id and d.pinned(db, 'identity', identity.id, d.scope(PROVIDER))
        audits = {a.action: a for a in db.scalars(select(Audit))}
        assert audits['user.create'].target_id == created.id
        assert audits['user.create'].details == {'via': 'im_discovery', 'login_enabled': False, 'role': 'member'}
        assert audits['im.discovery.approve'].details['created_user'] is True
        assert '小王' not in str([a.details for a in audits.values()])  # Names stay out of the audit trail.
    with database.begin() as db:
        assert im._enqueue(db, PROVIDER, 'before-resend', 'newbie', 'dm', '你好', False) == {'ok': True}
    with database() as db:
        run = db.scalar(select(Run).where(Run.user_id == result['user_id']))
        assert run and db.get(Conversation, run.conversation_id).owner_id == result['user_id']


def test_team_lead_role_is_allowed_and_applied(database):
    with database.begin() as db:
        result = onboard(db, pending(db), role='team_lead')
    with database() as db:
        assert db.get(User, result['user_id']).role == 'team_lead'


def test_the_existing_user_path_is_unchanged(database):
    with database.begin() as db:
        existing = db.scalar(select(User)).id
        row = pending(db, sender='colleague')
        result = d.approve(row.id, d.Approve(user_id=existing), root(), db)
    assert result['user_id'] == existing and result['created_user'] is False
    with database() as db:
        assert count(db, User) == 1 and count(db, Identity) == 2
        assert not [a for a in db.scalars(select(Audit)) if a.action == 'user.create']


@pytest.mark.parametrize('body', [
    {},
    {'user_id': 'someone', 'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team'}},
    {'new_user': {'name': '小王', 'role': 'org_admin', 'org_id': 'org', 'team_id': 'team'}},
    {'new_user': {'name': '小王', 'role': 'super_admin', 'org_id': 'org', 'team_id': 'team'}},
    {'new_user': {'name': '小王', 'org_id': 'org'}},
    {'new_user': {'name': '小王', 'team_id': 'team'}},
    {'new_user': {'name': '   ', 'org_id': 'org', 'team_id': 'team'}},
    {'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team', 'email': 'who@example.invalid'}},
    {'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team', 'password': 'x' * 12}},
    {'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team', 'active': False}},
    {'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team'}, 'group_id': 'g'},
    {'new_user': {'name': '小王', 'org_id': 'org', 'team_id': 'team'}, 'confirm_member': True},
])
def test_request_shape_is_validated(body):
    """Exactly one way to name the member; no credentials, no admin roles, no group fields alongside a new member."""
    with pytest.raises(ValidationError):
        d.Approve(**body)


@pytest.mark.parametrize('overrides,status', [
    ({'org_id': 'nowhere'}, 400),
    ({'team_id': 'nowhere'}, 400),
    ({'name': '\u200b\n'}, 400),
])
def test_unknown_directory_entries_and_blank_names_are_rejected_without_residue(database, overrides, status):
    with database.begin() as db:
        row_id = pending(db).id
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve(row_id, d.Approve(new_user=member(**overrides)), root(), db)
    assert caught.value.status_code == status
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (1, 1)
        assert not [a for a in db.scalars(select(Audit)) if a.action == 'user.create']


@pytest.mark.parametrize('archive,status', [('org', 409), ('team', 409), ('foreign', 403)])
def test_directory_rows_must_be_live_and_consistent(database, archive, status):
    with database.begin() as db:
        first, second = Organization(name='运营公司'), Organization(name='销售公司')
        db.add_all([first, second])
        db.flush()
        team = Department(name='广告部', org_id=first.id)
        db.add(team)
        db.flush()
        if archive == 'org':
            first.archived_at = now()
        if archive == 'team':
            team.archived_at = now()
        ids = (second.id if archive == 'foreign' else first.id, team.id)
        row_id = pending(db).id
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve(row_id, d.Approve(new_user=member(org_id=ids[0], team_id=ids[1])), root(), db)
    assert caught.value.status_code == status
    nothing_created(database)


def test_named_directory_entries_are_accepted(database):
    with database.begin() as db:
        organization = Organization(name='运营公司')
        db.add(organization)
        db.flush()
        department = Department(name='广告部', org_id=organization.id)
        db.add(department)
        db.flush()
        result = onboard(db, pending(db), org_id=organization.id, team_id=department.id)
    with database() as db:
        created = db.get(User, result['user_id'])
        assert (created.org_id, created.team_id) == (organization.id, department.id)


def test_a_sender_that_is_already_bound_must_use_the_existing_user_path(database):
    with database.begin() as db:
        row_id = pending(db, sender='sender', chat='dm2').id
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve(row_id, d.Approve(new_user=member()), root(), db)
    assert caught.value.status_code == 409
    nothing_created(database)


def test_a_replaced_application_cannot_be_onboarded(database, monkeypatch):
    with database.begin() as db:
        row_id = pending(db).id
    monkeypatch.setenv('FEISHU_APP_ID', 'rotated')
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve(row_id, d.Approve(new_user=member()), root(), db)
    assert caught.value.status_code == 409
    nothing_created(database)


@pytest.mark.parametrize('role', ['org_admin', 'team_lead', 'member'])
def test_only_super_admin_can_onboard(database, role):
    with database.begin() as db:
        row_id = pending(db).id
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve(row_id, d.Approve(new_user=member()), root(role), db)
    assert caught.value.status_code == 403
    nothing_created(database)


def test_unknown_discovery_is_not_found(database):
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            d.approve('missing', d.Approve(new_user=member()), root(), db)
    assert caught.value.status_code == 404


def test_double_submit_creates_exactly_one_member(database):
    with database.begin() as db:
        row_id = pending(db).id
    barrier = Barrier(2)

    def submit(_):
        barrier.wait()
        try:
            with database.begin() as db:
                return d.approve(row_id, d.Approve(new_user=member()), root(), db)['created_user']
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert sorted(map(str, outcomes)) == ['409', 'True']
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (2, 2)
        assert len([a for a in db.scalars(select(Audit)) if a.action == 'user.create']) == 1


def test_a_group_sender_is_bound_but_still_needs_the_group_registered(database):
    with database.begin() as db:
        row = pending(db, chat='newchat', group=True)
        result = onboard(db, row)
    with database.begin() as db:
        assert im._enqueue(db, PROVIDER, 'group-1', 'newbie', 'newchat', '你好', True)['pending']
        assert count(db, Run) == 0
    with database() as db:
        listed = d.discovered_groups(root(), db)
        assert [g['external_id'] for g in listed] == ['newchat']
        assert result['user_id'] in {m['id'] for m in listed[0]['members']}  # Ready for the existing group panel.
        current = {r['sender_id']: r['current_reason'] for r in d.discoveries(root(), db)}
        assert current['newbie'] == 'unknown_group'


def test_nothing_about_the_created_member_can_be_used_to_sign_in(database):
    for attempt in ('!', ':', '!:!', 'x' * 12, secrets.token_urlsafe(24)):
        assert not security.verify_password(attempt, LOCKED_PASSWORD)
    with database.begin() as db:
        result = onboard(db, pending(db))
    with database() as db:
        email = db.get(User, result['user_id']).email
    client = http(database)
    try:
        for attempt in ('!', 'x' * 12, secrets.token_urlsafe(24)):
            assert client.post('/api/auth/login', json={'email': email, 'password': attempt}).status_code == 401
        assert 'set-cookie' not in client.post('/api/auth/login', json={'email': email, 'password': '!'}).headers
    finally:
        client.close()
        main.app.dependency_overrides.clear()


def test_administrator_onboards_through_the_http_api_and_the_member_list_marks_login_less_users(database):
    password = secrets.token_urlsafe(24)
    with database.begin() as db:
        db.add(User(email='root@example.invalid', name='管理员', role='super_admin', active=True,
                    password_hash=security.hash_password(password)))
        row_id = pending(db).id
    client = http(database)
    try:
        assert client.post('/api/auth/login', json={'email': 'root@example.invalid', 'password': password}).status_code == 200
        shown = {r['id']: r for r in client.get('/api/im/discoveries').json()}
        assert shown[row_id]['status'] == 'pending' and shown[row_id]['nickname'] == '小王'
        body = {'new_user': {'name': '小王', 'role': 'member', 'org_id': 'org', 'team_id': 'team'}}
        response = client.post(f'/api/im/discoveries/{row_id}/approve', json=body)
        assert response.status_code == 200 and response.json()['created_user'] is True
        users = {u['name']: u for u in client.get('/api/users').json()}
        assert users['小王']['login_enabled'] is False and users['管理员']['login_enabled'] is True
        assert client.post(f'/api/im/discoveries/{row_id}/approve', json=body).status_code == 409
        for rejected in ({'new_user': body['new_user'], 'user_id': 'x'}, {'new_user': body['new_user'] | {'role': 'org_admin'}},
                         {'new_user': body['new_user'] | {'email': 'a@example.invalid'}}):
            assert client.post(f'/api/im/discoveries/{row_id}/approve', json=rejected).status_code == 422
        assert {r['id']: r for r in client.get('/api/im/discoveries').json()}[row_id]['status'] == 'authorized'
    finally:
        client.close()
        main.app.dependency_overrides.clear()
