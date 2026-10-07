"""Private chats and group chats are listed and refreshed separately (IM 集成 vs 协作群组); with no filter nothing changes."""
import secrets
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_im_nicknames import ALICE, BOB, CAROL, GROUP, clean_state, names, root, run, seed, transport  # noqa: F401
from test_im_postgres import database
from test_setup import http
from app import im_discovery as discovery, im_nicknames as nicknames, main, security
from app.models import Audit, IMDiscovery, User, now

DAVE = 'ou_dave000004'
PRIVATE_CHAT = 'oc_private01'
OTHER_GROUP = 'oc_group0002'


def listing(database, chat=None):
    with database() as db:
        return discovery.discoveries(root(), db, chat) if chat else discovery.discoveries(root(), db)


def senders(rows):
    return sorted(r['sender_id'] for r in rows)


def mixed(database):
    seed(database, [('feishu', ALICE, PRIVATE_CHAT, False, None), ('feishu', BOB, GROUP, True, None),
                    ('feishu', CAROL, 'oc_private02', False, None), ('feishu', DAVE, OTHER_GROUP, True, None)])


def test_the_list_can_be_limited_to_one_kind_and_is_unchanged_without_a_filter(database):
    mixed(database)
    assert senders(listing(database)) == sorted([ALICE, BOB, CAROL, DAVE])
    assert senders(listing(database, 'private')) == sorted([ALICE, CAROL])
    assert senders(listing(database, 'group')) == sorted([BOB, DAVE])
    assert {r['chat_type'] for r in listing(database, 'private')} == {'p2p'}
    assert {r['chat_type'] for r in listing(database, 'group')} == {'group'}


@pytest.mark.parametrize('bad', ['', 'all', 'p2p', 'GROUP', 'group;', "private' OR '1'='1", 'x' * 500])
def test_an_unknown_kind_is_refused_before_anything_is_read(database, bad):
    mixed(database)
    with database() as db:
        with pytest.raises(HTTPException) as refused:
            discovery.discoveries(root(), db, bad)
    assert refused.value.status_code == 422


def test_each_kind_gets_its_own_newest_500_so_a_busy_group_cannot_push_private_chats_out(database):
    base = now()
    with database.begin() as db:
        for index in range(520):
            db.add(IMDiscovery(provider='feishu', app_scope=discovery.scope('feishu'), sender_id=f'ou_group{index:05d}', chat_id=GROUP,
                               chat_type='group', reason='unknown_sender', last_seen=base))
        for index in range(3):
            db.add(IMDiscovery(provider='feishu', app_scope=discovery.scope('feishu'), sender_id=f'ou_solo{index:06d}', chat_id=f'oc_solo{index:05d}',
                               chat_type='p2p', reason='unknown_sender', last_seen=base - timedelta(hours=1)))
    assert len(listing(database)) == 500                       # Unfiltered: the newest 500 overall are all group rows.
    assert not [r for r in listing(database) if r['chat_type'] == 'p2p']
    assert len(listing(database, 'private')) == 3              # Filtered: the private chats are all there.
    assert len(listing(database, 'group')) == 500


def test_the_row_contents_do_not_depend_on_the_filter(database):
    mixed(database)
    everything = {r['id']: r for r in listing(database)}
    for chat in ('private', 'group'):
        for row in listing(database, chat):
            assert row == everything[row['id']]


def lookups(calls):
    return sorted(request.url.path for request in calls if '/contact/' in request.url.path or '/chats/' in request.url.path)


def feishu_routes(request):
    path = request.url.path
    if path.startswith('/open-apis/contact/v3/users/'):
        return 200, {'code': 0, 'data': {'user': {'name': '成员-' + path.rsplit('_', 1)[-1][-4:]}}}
    if path.startswith('/open-apis/im/v1/chats/') and path.endswith('/members'):
        return 200, {'code': 0, 'data': {'items': [], 'has_more': False}}
    if path.startswith('/open-apis/im/v1/chats/'):
        return 200, {'code': 0, 'data': {'name': '群-' + path.rsplit('_', 1)[-1][-4:]}}
    raise AssertionError(path)


def refreshed(database, kind):
    calls = []
    with database() as db:
        result = nicknames.refresh(db, transport(feishu_routes, calls), kind)
        db.commit()
    return result, calls


def test_the_private_refresh_looks_up_private_senders_only_and_never_a_group_or_its_name(database):
    mixed(database)
    result, calls = refreshed(database, 'private')
    assert result['status'] == 'ok' and result['resolved'] == 2 and result['chat_resolved'] == 0 and result['chat_unresolved'] == 0
    assert lookups(calls) == sorted([f'/open-apis/contact/v3/users/{ALICE}', f'/open-apis/contact/v3/users/{CAROL}'])
    got = names(database)
    assert got[(ALICE, 'p2p')] and got[(CAROL, 'p2p')] and got[(BOB, 'group')] is None and got[(DAVE, 'group')] is None
    with database() as db:
        assert db.scalar(select(IMDiscovery).where(IMDiscovery.chat_type == 'group', IMDiscovery.nickname.is_not(None))) is None
    assert {r['chat_name'] for r in listing(database)} == {None}


def test_the_group_refresh_looks_up_group_senders_and_group_names_and_leaves_private_chats_alone(database):
    mixed(database)
    result, calls = refreshed(database, 'group')
    assert result['status'] == 'ok' and result['resolved'] == 2 and result['chat_resolved'] == 2
    paths = lookups(calls)
    assert f'/open-apis/contact/v3/users/{BOB}' in paths and f'/open-apis/contact/v3/users/{DAVE}' in paths
    assert f'/open-apis/im/v1/chats/{GROUP}' in paths and f'/open-apis/im/v1/chats/{OTHER_GROUP}' in paths
    assert not [p for p in paths if ALICE in p or CAROL in p or PRIVATE_CHAT in p]
    got = names(database)
    assert got[(BOB, 'group')] and got[(DAVE, 'group')] and got[(ALICE, 'p2p')] is None and got[(CAROL, 'p2p')] is None
    assert {r['chat_name'] for r in listing(database, 'group')} == {'群-0001', '群-0002'}


def test_without_a_kind_the_refresh_does_both_exactly_as_before(database):
    mixed(database)
    result, calls = refreshed(database, None)
    assert result['resolved'] == 4 and result['chat_resolved'] == 2
    assert len([p for p in lookups(calls) if '/contact/' in p]) == 4 and len([p for p in lookups(calls) if '/chats/' in p]) == 2


def test_a_name_found_for_a_sender_is_written_to_every_row_of_that_sender(database):
    # One person writes in a private chat and in a group; either refresh names them everywhere they appear.
    seed(database, [('feishu', ALICE, PRIVATE_CHAT, False, None), ('feishu', ALICE, GROUP, True, None)])
    refreshed(database, 'private')
    assert names(database) == {(ALICE, 'p2p'): '成员-0001', (ALICE, 'group'): '成员-0001'}
    with database.begin() as db:
        for row in db.scalars(select(IMDiscovery)):
            row.nickname = None
    nicknames._failures.clear()
    refreshed(database, 'group')
    assert names(database) == {(ALICE, 'p2p'): '成员-0001', (ALICE, 'group'): '成员-0001'}


def test_the_private_refresh_never_uses_the_group_member_list_fallback(database):
    seed(database, [('feishu', BOB, GROUP, True, None), ('feishu', ALICE, PRIVATE_CHAT, False, None)])
    calls = []

    def routes(request):
        path = request.url.path
        if path.startswith('/open-apis/contact/v3/users/'):
            return 400, {'code': 41050, 'msg': 'no user authority'}
        raise AssertionError('private refresh must not read groups: ' + path)
    with database() as db:
        result = nicknames.refresh(db, transport(routes, calls), 'private')
        db.commit()
    assert result['unresolved'] == 1 and result['chat_resolved'] == 0
    assert lookups(calls) == [f'/open-apis/contact/v3/users/{ALICE}']


def test_the_endpoint_records_the_kind_in_the_audit_without_names_and_keeps_the_old_shape_without_one(database, monkeypatch):
    mixed(database)
    monkeypatch.setattr(nicknames, '_client', transport(feishu_routes, []))
    with database() as db:
        discovery.refresh_nicknames(root(), db, 'private')
        db.commit()
    with database() as db:
        audits = list(db.scalars(select(Audit).where(Audit.action == 'im.discovery.nickname_refresh')))
        assert audits[0].details == {'status': 'ok', 'resolved': 2, 'unresolved': 0, 'remaining': 0,
                                     'chat_resolved': 0, 'chat_unresolved': 0, 'chat_remaining': 0, 'chat': 'private'}
    nicknames._failures.clear()
    with database() as db:
        discovery.refresh_nicknames(root(), db)
        db.commit()
    with database() as db:
        latest = db.scalars(select(Audit).where(Audit.action == 'im.discovery.nickname_refresh').order_by(Audit.created_at.desc())).first()
        assert 'chat' not in latest.details and latest.details['chat_resolved'] == 2
        assert '成员' not in str(latest.details)


def test_an_unknown_kind_on_the_refresh_makes_no_outbound_call_and_writes_nothing(database, monkeypatch):
    mixed(database)
    calls = []
    monkeypatch.setattr(nicknames, '_client', transport(feishu_routes, calls))
    with database() as db:
        with pytest.raises(HTTPException) as refused:
            discovery.refresh_nicknames(root(), db, 'everything')
    assert refused.value.status_code == 422 and calls == []
    with database() as db:
        assert db.scalar(select(Audit.id).where(Audit.action == 'im.discovery.nickname_refresh')) is None
    assert set(names(database).values()) == {None}


def login(client, email, secret):
    assert client.post('/api/auth/login', json={'email': email, 'password': secret}).status_code == 200


def test_over_http_the_query_parameter_is_bound_validated_and_super_admin_only(database, monkeypatch):
    mixed(database)
    monkeypatch.setattr(nicknames, '_client', transport(feishu_routes, []))
    secrets_by_role = {role: secrets.token_urlsafe(18) for role in ('super_admin', 'org_admin')}
    with database.begin() as db:
        for role, secret in secrets_by_role.items():
            db.add(User(email=f'{role}@example.invalid', name=role, role=role, active=True, org_id='org', team_id='team',
                        password_hash=security.hash_password(secret)))
    boss, other = http(database), TestClient(main.app)
    login(boss, 'super_admin@example.invalid', secrets_by_role['super_admin'])
    login(other, 'org_admin@example.invalid', secrets_by_role['org_admin'])
    assert senders(boss.get('/api/im/discoveries?chat=private').json()) == sorted([ALICE, CAROL])
    assert senders(boss.get('/api/im/discoveries?chat=group').json()) == sorted([BOB, DAVE])
    assert len(boss.get('/api/im/discoveries').json()) == 4
    assert boss.get('/api/im/discoveries?chat=bogus').status_code == 422
    assert boss.post('/api/im/discoveries/nicknames?chat=bogus').status_code == 422
    assert boss.post('/api/im/discoveries/nicknames?chat=private').json()['resolved'] == 2
    assert other.get('/api/im/discoveries?chat=group').status_code == 403
    assert other.post('/api/im/discoveries/nicknames?chat=group').status_code == 403
