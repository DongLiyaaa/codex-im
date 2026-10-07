"""Hub-side 可用人员 for personal platform authorization, against an isolated PostgreSQL."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_im_postgres import database
from test_platform_auth import configure, device
from test_platform_broker import request as start_authorization
from app import platform_access as access, platform_auth as pa, platform_broker as broker, platform_workspace as workspace
from app.models import Department, Identity, Organization, PlatformAccess, PlatformAuthJob, PlatformConnection, User, now


def admin(db):
    user = db.scalar(select(User).where(User.role == 'super_admin'))
    if user is None:
        user = User(email='root@example.invalid', name='管理员', role='super_admin', active=True, password_hash='unused')
        db.add(user)
        db.flush()
    return user


def save(database, scope, ids, revision=None, provider='feishu'):
    with database.begin() as db:
        current = access.view(db, provider)['revision'] if revision is None else revision
        return access.save(db, admin(db), provider, access.Update(revision=current, user_scope=scope, user_ids=ids))


def member(database):
    with database() as db:
        return db.scalar(select(User).where(User.role == 'member')).id


@pytest.fixture
def ready(database, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(pa, 'begin', device)
    monkeypatch.setattr(broker, 'dispatch', lambda *args: None)
    return database


def test_everyone_may_authorize_until_a_list_is_saved(ready):
    _, _, result = start_authorization(ready)
    assert result['state'] == 'starting'
    with ready() as db:
        assert db.get(PlatformAccess, 'feishu') is None


def test_someone_off_the_list_cannot_start_and_is_told_who_decides(ready):
    save(ready, 'specified', [])
    user_id, _, result = start_authorization(ready)
    assert (result['state'], result['error_code'], result['next_action']) == ('user_not_allowed', 'USER_NOT_ALLOWED', 'contact_hub_admin')
    assert '可用人员' in result['message']
    with ready() as db:
        job = db.get(PlatformAuthJob, (user_id, 'feishu'))
        assert job is None or job.phase == 'done'
    save(ready, 'specified', [user_id])
    with ready.begin() as db:
        assert broker.operate(db, db.get(User, user_id), 'feishu', 'start')['state'] == 'starting'


def test_removing_someone_discards_their_stored_authorization(ready):
    user_id, _, _ = start_authorization(ready)
    with ready.begin() as db:
        row = db.get(PlatformConnection, (user_id, 'feishu'))
        row.state, row.encrypted, row.expires_at = 'connected', pa.seal({'access_token': 'USER-TOKEN', 'scope': ' '.join(pa.REQUIRED['feishu'])}), now() + timedelta(hours=1)
        db.get(PlatformAuthJob, (user_id, 'feishu')).phase = 'done'
        assert workspace.personal_token(db, db.get(User, user_id), 'feishu') == 'USER-TOKEN'
        generation = db.get(PlatformAuthJob, (user_id, 'feishu')).generation
    saved = save(ready, 'specified', [])
    assert (saved['user_scope'], saved['user_ids'], saved['revision']) == ('specified', [], 1)
    with ready() as db:
        row, job = db.get(PlatformConnection, (user_id, 'feishu')), db.get(PlatformAuthJob, (user_id, 'feishu'))
        assert (row.state, row.encrypted, job.phase, job.error) == ('user_not_allowed', '', 'done', 'USER_NOT_ALLOWED')
        assert job.generation != generation  # An in-flight worker step discards its result.


def test_workspace_refuses_people_off_the_list_even_with_a_stored_token(ready):
    user_id = member(ready)
    with ready.begin() as db:
        row = pa.locked(db, user_id, 'dingtalk')
        row.state, row.encrypted, row.expires_at = 'connected', pa.seal({'access_token': 'USER-TOKEN'}), now() + timedelta(hours=1)
        db.add(PlatformAccess(provider='dingtalk', user_scope='specified', user_ids=[]))
    with ready() as db:
        user = db.get(User, user_id)
        assert workspace.personal_token(db, user, 'dingtalk') is None
        with pytest.raises(workspace.WorkspaceError) as caught:
            workspace._dingtalk_job(db, user, {})
        assert (caught.value.code, caught.value.next_action) == ('user_not_allowed', 'contact_admin')
        assert caught.value.code in workspace.MESSAGES
    with ready.begin() as db:
        # Status, the next time anyone looks, also drops what the list no longer allows.
        assert broker.status(db, db.get(User, user_id), 'dingtalk')['state'] == 'user_not_allowed'
        assert db.get(PlatformConnection, (user_id, 'dingtalk')).encrypted == ''


def test_stale_revision_and_unknown_people_are_refused(ready):
    save(ready, 'all', [])
    with pytest.raises(HTTPException) as caught:
        save(ready, 'specified', [], revision=0)
    assert caught.value.status_code == 409
    with pytest.raises(HTTPException) as caught:
        save(ready, 'specified', ['no-such-user'])
    assert caught.value.status_code == 422
    # Everyone again: the remembered names are dropped rather than kept for later.
    save(ready, 'specified', [member(ready)])
    assert save(ready, 'all', [member(ready)])['user_ids'] == []


def test_only_super_administrators_see_or_change_the_list(ready):
    from fastapi.testclient import TestClient
    from app import main, security
    with ready.begin() as db:
        root_id = admin(db).id
    current = {'id': member(ready)}

    def session():
        with ready.begin() as db:
            yield db

    def user():
        with ready() as db:
            return db.get(User, current['id'])
    main.app.dependency_overrides[main.get_db] = session
    main.app.dependency_overrides[security.current_user] = user
    try:
        client = TestClient(main.app)
        assert client.get('/api/integrations/oauth/dingtalk/access').status_code == 403
        assert client.put('/api/integrations/oauth/dingtalk/access', json={'revision': 0, 'user_scope': 'all'}).status_code == 403
        current['id'] = root_id
        seen = client.get('/api/integrations/oauth/dingtalk/access').json()
        assert {k: seen[k] for k in ('provider', 'revision', 'user_scope', 'user_ids')} == {'provider': 'dingtalk', 'revision': 0, 'user_scope': 'all', 'user_ids': []}
        assert root_id in [p['id'] for p in seen['people']]
        saved = client.put('/api/integrations/oauth/dingtalk/access', json={'revision': 0, 'user_scope': 'specified', 'user_ids': [root_id]})
        assert saved.status_code == 200 and saved.json()['user_ids'] == [root_id]
        assert client.put('/api/integrations/oauth/dingtalk/access', json={'revision': 1, 'user_scope': 'some'}).status_code == 422
        assert client.get('/api/integrations/oauth/other/access').status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_people_are_shown_as_an_administrator_knows_them(ready):
    with ready.begin() as db:
        org, team = Organization(name='公司'), None
        db.add(org)
        db.flush()
        team = Department(org_id=org.id, name='运营')
        db.add(team)
        db.flush()
        bound = User(email='im-0001@im.invalid', name='冬离', role='member', org_id=org.id, team_id=team.id, active=True, password_hash='!')
        unbound = User(email='lan@example.com', name='蓝海明', role='org_admin', org_id=org.id, active=True, password_hash='x')
        db.add_all([bound, unbound])
        db.flush()
        db.add(Identity(provider='dingtalk', external_user_id='326850072533581332', user_id=bound.id, corp_id='ding-corp'))
        db.add(Identity(provider='feishu', external_user_id='ou_x', user_id=unbound.id))
        pa.locked(db, bound.id, 'dingtalk').state = 'pending'
        ids = bound.id, unbound.id
    with ready() as db:
        people = {p['id']: p for p in access.view(db, 'dingtalk')['people']}
    assert people[ids[0]] == {'id': ids[0], 'name': '冬离', 'role': 'member', 'active': True, 'email': None, 'organization': '公司',
                              'department': '运营', 'account': '326850072533581332', 'authorization': 'pending'}
    assert (people[ids[1]]['email'], people[ids[1]]['department'], people[ids[1]]['account'], people[ids[1]]['authorization']) == ('lan@example.com', None, None, None)
    with ready() as db:
        order = [p['id'] for p in access.view(db, 'dingtalk')['people']]
    assert order.index(ids[0]) < order.index(ids[1])  # People who can authorize come first.
