"""Opt-in automatic onboarding: only verified same-organization private senders, only member, capped, fail-safe."""
import asyncio
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from test_im_postgres import database
from test_setup import http
from app import im, im_connections as connections, im_discovery as d, im_inbound, im_onboarding as auto, main, security
from app.models import (Audit, Conversation, Department, IMDiscovery, IMOnboardingPolicy, Identity, LOCKED_PASSWORD,
                        Organization, Run, User, now)


def root(role='super_admin'):
    return SimpleNamespace(id='root', role=role, active=True, org_id=None, team_id=None)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def enable(db, provider='feishu', cap=20, org='org', team='team', enabled=True):
    return auto.save(provider, auto.PolicyBody(enabled=enabled, org_id=org, team_id=team, daily_cap=cap), root(), db)


def first_message(db, sender='newcomer', event='e1', provider='feishu', chat=None, group=False, **options):
    options.setdefault('sender_internal', True)
    return im._enqueue(db, provider, event, sender, chat or 'dm-' + sender, 'hello', group, **options)


def untouched(database):
    with database() as db:
        assert (count(db, User), count(db, Identity), count(db, Run)) == (1, 1, 0)


def test_nothing_happens_until_an_administrator_turns_it_on(database):
    with database.begin() as db:
        assert first_message(db)['pending']
    untouched(database)
    with database() as db:
        assert db.scalar(select(IMDiscovery.reason)) == 'unknown_sender'
        assert db.get(IMOnboardingPolicy, 'feishu') is None
        assert [(p['enabled'], p['daily_cap'], p['used_24h'], p['valid']) for p in auto.policies(root(), db)] == [(False, 20, 0, None)] * 2


def test_a_verified_private_sender_becomes_a_member_and_is_served_at_once(database):
    with database.begin() as db:
        enable(db)
    with database.begin() as db:
        assert first_message(db) == {'ok': True}
    with database() as db:
        member = db.scalar(select(User).where(User.password_hash == LOCKED_PASSWORD))
        assert (member.name, member.role, member.org_id, member.team_id, member.active) == ('飞书用户 comer'[:0] + '飞书用户 ewcomer', 'member', 'org', 'team', True) \
            or member.name == '飞书用户 wcomer'
        identity = db.scalar(select(Identity).where(Identity.external_user_id == 'newcomer'))
        assert identity.user_id == member.id and d.pinned(db, 'identity', identity.id, d.scope('feishu'))
        assert db.scalar(select(Run.user_id)) == member.id and count(db, IMDiscovery) == 0
        created = [a for a in db.scalars(select(Audit)) if a.action in ('user.create', 'im.auto_onboard.feishu')]
        assert sorted(a.action for a in created) == ['im.auto_onboard.feishu', 'user.create']
        assert next(a for a in created if a.action == 'user.create').details == {'via': 'im_auto', 'login_enabled': False, 'role': 'member'}
        assert all(a.actor_id is None for a in created)
        assert 'newcomer' not in str([a.details for a in created])
    with database.begin() as db:
        assert first_message(db, event='e2') in ({'ok': True}, {'ok': True, 'busy': True})
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (2, 2)


def test_dingtalk_uses_the_display_name_from_the_event(database):
    with database.begin() as db:
        enable(db, 'dingtalk')
    with database.begin() as db:
        assert first_message(db, provider='dingtalk', nickname='  钉钉小王 ') == {'ok': True}
    with database() as db:
        assert db.scalar(select(User.name).where(User.password_hash == LOCKED_PASSWORD)) == '钉钉小王'


@pytest.mark.parametrize('options', [
    {'sender_internal': False},
    {'group': True},
    {'ingress_reason': 'unsupported_reply_target'},
])
def test_everything_else_stays_a_manual_decision(database, options):
    with database.begin() as db:
        enable(db)
    with database.begin() as db:
        assert first_message(db, **options)['pending']
    untouched(database)
    with database() as db:
        assert count(db, IMDiscovery) == 1


def test_a_missing_policy_table_never_breaks_message_handling(database):
    """E.g. a message processed by a freshly started process before the table exists."""
    with database.begin() as db:
        IMOnboardingPolicy.__table__.drop(db.connection())
    with database.begin() as db:
        assert first_message(db)['pending']
        assert first_message(db, 'known-sender', 'e-known', sender_internal=False)['pending']
    with database() as db:
        assert (count(db, User), count(db, Identity), count(db, IMDiscovery)) == (1, 1, 2)


def test_a_policy_for_one_platform_never_applies_to_the_other(database):
    with database.begin() as db:
        enable(db, 'dingtalk')
    with database.begin() as db:
        assert first_message(db, provider='feishu')['pending']
    untouched(database)


def test_the_daily_cap_stops_it_and_only_counts_recent_onboardings_of_the_same_platform(database):
    with database.begin() as db:
        enable(db, cap=2)
        db.add(Audit(action='im.auto_onboard.feishu', details={}, created_at=now() - timedelta(hours=25)))
        db.add(Audit(action='im.auto_onboard.dingtalk', details={}))
    with database.begin() as db:
        assert first_message(db, 'a', 'ea') == {'ok': True}
        assert first_message(db, 'b', 'eb') == {'ok': True}
        assert first_message(db, 'c', 'ec')['pending']
    with database() as db:
        assert {i.external_user_id for i in db.scalars(select(Identity))} == {'sender', 'a', 'b'}
        assert [(p['used_24h'], p['daily_cap']) for p in auto.policies(root(), db)] == [(2, 2), (1, 20)]
    with database.begin() as db:
        enable(db, cap=3)
        assert first_message(db, 'c', 'ed') == {'ok': True}


def test_a_policy_pointing_at_a_deleted_department_falls_back_without_residue(database):
    with database.begin() as db:
        organization = Organization(name='运营公司')
        db.add(organization)
        db.flush()
        department = Department(name='广告部', org_id=organization.id)
        db.add(department)
        db.flush()
        enable(db, org=organization.id, team=department.id)
        ids = (organization.id, department.id)
    with database.begin() as db:
        assert first_message(db, 'early', 'e0') == {'ok': True}
    with database.begin() as db:
        db.get(Department, ids[1]).archived_at = now()
    with database.begin() as db:
        assert first_message(db, 'late', 'e1')['pending']
    with database() as db:
        assert 'late' not in {i.external_user_id for i in db.scalars(select(Identity))}
        assert count(db, User) == 2 and db.get(IMOnboardingPolicy, 'feishu').enabled is True
        assert [p['valid'] for p in auto.policies(root(), db)] == [False, None]
    with database.begin() as db:  # Switching it off must work even though its directory entry is gone.
        enable(db, org=ids[0], team=ids[1], enabled=False)
    with database() as db:
        assert db.get(IMOnboardingPolicy, 'feishu').enabled is False


def test_two_first_messages_at_once_create_exactly_one_member(database):
    with database.begin() as db:
        enable(db)
    barrier = Barrier(2)

    def send(event):
        barrier.wait()
        with database.begin() as db:
            return first_message(db, 'twin', event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, ['x1', 'x2']))
    assert all(r.get('ok') for r in results)
    with database() as db:
        assert (count(db, User), count(db, Identity)) == (2, 2)
        assert len([a for a in db.scalars(select(Audit)) if a.action == 'im.auto_onboard.feishu']) == 1


@pytest.mark.parametrize('body,problem', [
    ({'enabled': True}, 'organization and department'),
    ({'enabled': True, 'org_id': 'org'}, 'organization and department'),
    ({'enabled': False, 'team_id': 'team'}, 'go together'),
    ({'enabled': True, 'org_id': 'org', 'team_id': 'team', 'daily_cap': 0}, 'greater'),
    ({'enabled': True, 'org_id': 'org', 'team_id': 'team', 'daily_cap': auto.MAX_CAP + 1}, 'less'),
    ({'enabled': True, 'org_id': 'org', 'team_id': 'team', 'role': 'org_admin'}, 'Extra'),
    ({'enabled': True, 'org_id': 'org', 'team_id': 'team', 'daily_cap': 'many'}, 'integer'),
    ({'org_id': 'org', 'team_id': 'team'}, 'required'),
])
def test_policy_requests_are_validated(body, problem):
    with pytest.raises(ValidationError) as caught:
        auto.PolicyBody(**body)
    assert problem.lower() in str(caught.value).lower()


@pytest.mark.parametrize('org,team,status', [('nowhere', 'team', 400), ('org', 'nowhere', 400)])
def test_an_enabled_policy_must_point_at_live_directory_entries(database, org, team, status):
    with pytest.raises(HTTPException) as caught:
        with database.begin() as db:
            enable(db, org=org, team=team)
    assert caught.value.status_code == status
    with database() as db:
        assert db.get(IMOnboardingPolicy, 'feishu') is None


@pytest.mark.parametrize('role', ['org_admin', 'team_lead', 'member'])
def test_only_super_admin_can_read_or_change_the_policy(role):
    with pytest.raises(HTTPException) as caught:
        d.admin(root(role))
    assert caught.value.status_code == 403


def test_saving_is_audited_and_returns_the_current_view(database):
    with database.begin() as db:
        view = enable(db, cap=5)
    assert (view['provider'], view['enabled'], view['daily_cap'], view['used_24h'], view['valid']) == ('feishu', True, 5, 0, True)
    with database() as db:
        saved = [a for a in db.scalars(select(Audit)) if a.action == 'im.onboarding_policy.update']
        assert [(a.actor_id, a.target_id, a.details) for a in saved] == [
            ('root', 'feishu', {'enabled': True, 'org_id': 'org', 'team_id': 'team', 'daily_cap': 5})]


@pytest.mark.parametrize('header,sender,expected', [
    ({'tenant_key': 'T1'}, {'tenant_key': 'T1'}, True),
    ({'tenant_key': 'T1'}, {'tenant_key': 'T2'}, False),
    ({'tenant_key': 'T1'}, {}, False),
    ({}, {'tenant_key': 'T1'}, False),
    ({'tenant_key': ''}, {'tenant_key': ''}, False),
    ({'tenant_key': 7}, {'tenant_key': 7}, False),
    (None, None, False),
])
def test_feishu_sender_must_belong_to_the_event_tenant(header, sender, expected):
    assert im_inbound.feishu_internal(header, sender) is expected


@pytest.mark.parametrize('payload,expected', [
    ({'senderCorpId': 'ding1', 'chatbotCorpId': 'ding1'}, True),
    ({'senderCorpId': 'ding1', 'chatbotCorpId': 'ding2'}, False),
    ({'senderCorpId': 'ding1'}, False),
    ({'chatbotCorpId': 'ding1'}, False),
    ({'senderCorpId': '', 'chatbotCorpId': ''}, False),
    ({}, False),
    (None, False),
])
def test_dingtalk_sender_must_belong_to_the_robots_organization(payload, expected):
    assert im_inbound.dingtalk_internal(payload) is expected


def feishu_event(tenant='T1', sender_tenant='T1', sender='newcomer', kind='p2p', message_id='m1'):
    message = {'message_id': message_id, 'chat_id': 'oc_' + sender, 'chat_type': kind, 'message_type': 'text',
               'content': json.dumps({'text': 'hello'})}
    if kind == 'group':
        message['mentions'] = [{'key': '@_user_1', 'id': {'open_id': 'ou_test_bot'}, 'name': 'bot'}]
    header = {'event_type': 'im.message.receive_v1'} | ({'tenant_key': tenant} if tenant else {})
    who = {'sender_type': 'user', 'sender_id': {'open_id': sender}} | ({'tenant_key': sender_tenant} if sender_tenant else {})
    return {'header': header, 'event': {'sender': who, 'message': message}}


@pytest.mark.parametrize('tenant,sender_tenant,kind,onboarded', [
    ('T1', 'T1', 'p2p', True), ('T1', 'T2', 'p2p', False), ('T1', None, 'p2p', False), (None, 'T1', 'p2p', False),
    ('T1', 'T1', 'group', False)])
def test_feishu_connection_only_onboards_a_verified_private_sender(database, tenant, sender_tenant, kind, onboarded):
    with database.begin() as db:
        enable(db)
    result = connections.receive_feishu(feishu_event(tenant, sender_tenant, kind=kind), database)
    assert bool(result.get('pending')) is not onboarded
    with database() as db:
        assert (count(db, User), count(db, Run)) == ((2, 1) if onboarded else (1, 0))


def test_feishu_webhook_path_uses_the_same_rule(database, monkeypatch):
    async def body(_):
        return b''

    async def call(event):
        request = SimpleNamespace(headers={})
        with database.begin() as db:
            return await im._feishu_callback(request, db)

    monkeypatch.setattr(im, 'transport', lambda provider: 'webhook')
    monkeypatch.setattr(im, '_body', body)
    with database.begin() as db:
        enable(db)
    for event, onboarded in ((feishu_event('T1', 'T2', sender='outsider', message_id='w1'), False),
                             (feishu_event(sender='insider', message_id='w2'), True)):
        monkeypatch.setattr(im, 'verify_feishu', lambda raw, headers, event=event: event)
        result = asyncio.run(call(event))
        assert bool(result.get('pending')) is not onboarded
    with database() as db:
        assert {i.external_user_id for i in db.scalars(select(Identity))} == {'sender', 'insider'}


def ding_event(sender='ding-new', same_corp=True, kind='1', nick='钉钉小王'):
    return {'msgId': 'd1', 'msgtype': 'text', 'text': {'content': 'hello'}, 'senderStaffId': sender, 'conversationId': 'cid_' + sender,
            'conversationType': kind, 'robotCode': 'robot', 'senderNick': nick, 'senderCorpId': 'corp-a',
            'chatbotCorpId': 'corp-a' if same_corp else 'corp-b'}


@pytest.mark.parametrize('same_corp,kind,onboarded', [(True, '1', True), (False, '1', False), (True, '2', False)])
def test_dingtalk_stream_only_onboards_a_verified_private_sender(database, monkeypatch, same_corp, kind, onboarded):
    monkeypatch.setenv('DINGTALK_ROBOT_CODE', 'robot')
    with database.begin() as db:
        enable(db, 'dingtalk')
    result = connections.receive_dingtalk(ding_event(same_corp=same_corp, kind=kind), database)
    assert bool(result.get('pending')) is not onboarded
    with database() as db:
        names = [u.name for u in db.scalars(select(User).where(User.password_hash == LOCKED_PASSWORD))]
        assert names == (['钉钉小王'] if onboarded else [])


def test_http_administrator_configures_the_policy_and_reads_it_back(database):
    password = secrets.token_urlsafe(20)
    with database.begin() as db:
        db.add(User(email='root@example.invalid', name='管理员', role='super_admin', active=True, password_hash=security.hash_password(password)))
    client = http(database)
    try:
        assert client.get('/api/im/onboarding-policy').status_code == 401
        assert client.post('/api/auth/login', json={'email': 'root@example.invalid', 'password': password}).status_code == 200
        assert [p['enabled'] for p in client.get('/api/im/onboarding-policy').json()] == [False, False]
        saved = client.put('/api/im/onboarding-policy/feishu', json={'enabled': True, 'org_id': 'org', 'team_id': 'team', 'daily_cap': 7})
        assert saved.status_code == 200 and (saved.json()['enabled'], saved.json()['daily_cap'], saved.json()['valid']) == (True, 7, True)
        assert [p['enabled'] for p in client.get('/api/im/onboarding-policy').json()] == [True, False]
        assert client.put('/api/im/onboarding-policy/other', json={'enabled': False}).status_code == 422
        assert client.put('/api/im/onboarding-policy/feishu', json={'enabled': True}).status_code == 422
        assert client.put('/api/im/onboarding-policy/feishu', json={'enabled': True, 'org_id': 'org', 'team_id': 'team', 'role': 'org_admin'}).status_code == 422
        assert client.put('/api/im/onboarding-policy/feishu', json={'enabled': True, 'org_id': 'x', 'team_id': 'team'}).status_code == 400
        assert [p['enabled'] for p in client.get('/api/im/onboarding-policy').json()] == [True, False]
        assert client.put('/api/im/onboarding-policy/feishu', json={'enabled': False}).json()['enabled'] is False
    finally:
        client.close()
        main.app.dependency_overrides.clear()
