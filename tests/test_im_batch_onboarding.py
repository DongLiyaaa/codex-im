"""Batch onboarding: each sender is handled on its own, so one conflict never blocks or undoes the others."""
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
from app.models import Audit, IMDiscovery, Identity, LOCKED_PASSWORD, Organization, Department, User

PROVIDER = 'feishu'


def root(role='super_admin'):
    return SimpleNamespace(id='root', role=role, active=True, org_id=None, team_id=None)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def discover(db, sender, chat=None, provider=PROVIDER, nickname=None):
    d.record(db, provider, d.scope(provider), sender, chat or 'dm-' + sender, False, 'unknown_sender', nickname)
    db.flush()
    return db.scalar(select(IMDiscovery).where(IMDiscovery.provider == provider, IMDiscovery.sender_id == sender,
                                                IMDiscovery.chat_id == (chat or 'dm-' + sender))).id


def batch(rows, names=None, **overrides):
    items = [{'discovery_id': row, 'name': (names or {}).get(row, f'成员{index}')} for index, row in enumerate(rows)]
    return d.BatchOnboard(**({'items': items, 'org_id': 'org', 'team_id': 'team'} | overrides))


def run(db, body, who=None):
    return d.onboard_batch(body, who or root(), db)


def nothing_created(database):
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (1, 1)


def test_everyone_in_the_batch_becomes_a_member_with_one_audit_summary(database):
    with database.begin() as db:
        rows = [discover(db, f'person{i}') for i in range(3)]
        result = run(db, batch(rows, role='team_lead'))
    assert (result['created'], result['failed']) == (3, 0)
    assert [entry['ok'] for entry in result['results']] == [True] * 3 and [entry['id'] for entry in result['results']] == rows
    with database() as db:
        members = list(db.scalars(select(User).where(User.password_hash == LOCKED_PASSWORD).order_by(User.name)))
        assert [(m.name, m.role, m.org_id, m.team_id, m.active) for m in members] == [
            (f'成员{i}', 'team_lead', 'org', 'team', True) for i in range(3)]
        assert count(db, Identity) == 4
        assert all(d.pinned(db, 'identity', i.id, d.scope(PROVIDER)) for i in db.scalars(select(Identity)))
        summary = [a for a in db.scalars(select(Audit)) if a.action == 'im.discovery.onboard_batch']
        assert [(a.target_id, a.details) for a in summary] == [(PROVIDER, {'requested': 3, 'created': 3})]
        assert not any(name in str([a.details for a in db.scalars(select(Audit))]) for name in ('成员0', '成员1'))
    with database.begin() as db:
        for index in range(3):
            assert im._enqueue(db, PROVIDER, f'after{index}', f'person{index}', f'dm-person{index}', 'hi', False) == {'ok': True}


def test_failures_are_reported_per_sender_and_never_undo_the_successes(database):
    with database.begin() as db:
        good_first = discover(db, 'first')
        already = discover(db, 'sender', 'second-chat')  # Bound by the fixture: must use the existing-user path.
        twin_a, twin_b = discover(db, 'twin', 'chat-a'), discover(db, 'twin', 'chat-b')
        invisible = discover(db, 'ghost')
        good_last = discover(db, 'last')
        rows = [good_first, already, twin_a, twin_b, 'missing', invisible, good_last]
        result = run(db, batch(rows, names={invisible: '\u200b'}))
    outcome = {entry['id']: (entry['ok'], entry.get('status')) for entry in result['results']}
    assert outcome == {good_first: (True, None), already: (False, 409), twin_a: (True, None), twin_b: (False, 409),
                       'missing': (False, 404), invisible: (False, 400), good_last: (True, None)}
    assert (result['created'], result['failed']) == (3, 4)
    with database() as db:
        assert {u.name for u in db.scalars(select(User).where(User.password_hash == LOCKED_PASSWORD))} == {'成员0', '成员2', '成员6'}
        assert count(db, Identity) == 4  # The fixture's own identity plus the three new ones; no residue from failures.
        assert sorted(a.action for a in db.scalars(select(Audit))).count('user.create') == 3


def test_an_unknown_directory_entry_fails_every_item_without_creating_anything(database):
    with database.begin() as db:
        rows = [discover(db, 'one'), discover(db, 'two')]
        result = run(db, batch(rows, team_id='nowhere'))
    assert (result['created'], result['failed']) == (0, 2)
    assert {entry['status'] for entry in result['results']} == {400}
    nothing_created(database)


def test_a_batch_cannot_mix_platforms(database):
    with database.begin() as db:
        feishu, dingtalk = discover(db, 'one'), discover(db, 'two', provider='dingtalk')
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            run(db, batch([feishu, dingtalk]))
    assert caught.value.status_code == 400
    nothing_created(database)


@pytest.mark.parametrize('body', [
    {'items': []},
    {'items': [{'discovery_id': str(i), 'name': 'x'} for i in range(d.MAX_BATCH + 1)]},
    {'items': [{'discovery_id': 'a', 'name': 'x'}, {'discovery_id': 'a', 'name': 'y'}]},
    {'items': [{'discovery_id': 'a', 'name': '   '}]},
    {'items': [{'discovery_id': 'a', 'name': 'x', 'email': 'who@example.invalid'}]},
    {'items': [{'discovery_id': 'a', 'name': 'x'}], 'role': 'org_admin'},
    {'items': [{'discovery_id': 'a', 'name': 'x'}], 'role': 'super_admin'},
    {'items': [{'discovery_id': 'a', 'name': 'x'}], 'org_id': None},
    {'items': [{'discovery_id': 'a', 'name': 'x'}], 'team_id': None},
    {'items': [{'discovery_id': 'a', 'name': 'x'}], 'password': 'x' * 12},
])
def test_request_shape_is_validated(body):
    with pytest.raises(ValidationError):
        d.BatchOnboard(**({'org_id': 'org', 'team_id': 'team'} | body))


@pytest.mark.parametrize('role', ['org_admin', 'team_lead', 'member'])
def test_only_super_admin_can_onboard_in_batch(database, role):
    with database.begin() as db:
        rows = [discover(db, 'one')]
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            run(db, batch(rows), root(role))
    assert caught.value.status_code == 403
    nothing_created(database)


def test_a_replaced_application_cannot_be_batch_onboarded(database, monkeypatch):
    with database.begin() as db:
        rows = [discover(db, 'one')]
    monkeypatch.setenv('FEISHU_APP_ID', 'rotated')
    with database.begin() as db:
        result = run(db, batch(rows))
    assert (result['created'], result['results'][0]['status']) == (0, 409)
    nothing_created(database)


def test_overlapping_batches_in_opposite_order_create_each_member_once(database):
    with database.begin() as db:
        rows = [discover(db, name) for name in ('alpha', 'beta', 'gamma', 'delta')]
    barrier, orders = Barrier(2), [rows, list(reversed(rows))]

    def submit(order):
        barrier.wait()
        with database.begin() as db:
            return run(db, batch(order))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(submit, orders))
    assert first['created'] + second['created'] == 4
    assert first['failed'] + second['failed'] == 4  # Every sender lost exactly once to the other batch.
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (5, 5)
        assert len({i.external_user_id for i in db.scalars(select(Identity))}) == 5


def test_http_batch_returns_per_sender_results(database):
    password = secrets.token_urlsafe(20)
    with database.begin() as db:
        db.add(User(email='root@example.invalid', name='管理员', role='super_admin', active=True, password_hash=security.hash_password(password)))
        rows = [discover(db, 'one', nickname='小王'), discover(db, 'sender', 'again')]
    client = http(database)
    try:
        assert client.post('/api/auth/login', json={'email': 'root@example.invalid', 'password': password}).status_code == 200
        payload = {'items': [{'discovery_id': rows[0], 'name': '小王'}, {'discovery_id': rows[1], 'name': '重复'}], 'org_id': 'org', 'team_id': 'team'}
        response = client.post('/api/im/discoveries/onboard-batch', json=payload)
        assert response.status_code == 200
        body = response.json()
        assert (body['created'], body['failed']) == (1, 1)
        assert [(r['ok'], r.get('status')) for r in body['results']] == [(True, None), (False, 409)]
        assert body['results'][0]['user_id'] and 'error' in body['results'][1]
        assert client.post('/api/im/discoveries/onboard-batch', json=payload | {'role': 'org_admin'}).status_code == 422
        assert client.post('/api/im/discoveries/onboard-batch', json={**payload, 'items': []}).status_code == 422
        users = {u['name']: u for u in client.get('/api/users').json()}
        assert users['小王']['login_enabled'] is False
    finally:
        client.close()
        main.app.dependency_overrides.clear()
