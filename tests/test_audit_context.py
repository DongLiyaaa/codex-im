"""Audit rows name the person (with their Feishu/DingTalk nickname) and the chat, and only within the viewer's reach."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from test_im_postgres import database
from app import audit_context, main
from app.models import (Audit, Conversation, Group, IMDiscovery, IMEvent, Identity, Message, Run, User, now, uid)


def viewer(role='super_admin', org='org', team='team', identifier='viewer'):
    return SimpleNamespace(id=identifier, role=role, active=True, org_id=org, team_id=team)


def person(db, name, org='org', team='team', role='member'):
    user = User(email=f'{uid()}@example.invalid', name=name, role=role, org_id=org, team_id=team, active=True, password_hash='unused')
    db.add(user)
    db.flush()
    return user


def known_as(db, user, provider, sender, nickname=None, seen=None):
    db.add(Identity(provider=provider, external_user_id=sender, user_id=user.id))
    if nickname is not None:
        db.add(IMDiscovery(provider=provider, app_scope='scope', sender_id=sender, chat_id='c-' + sender, chat_type='p2p',
                           nickname=nickname, reason='unbound', last_seen=seen or now()))
    db.flush()


def chat_group(db, name='产品群', provider='feishu', org='org', team='team', members=(), archived=False):
    group = Group(name=name, org_id=org, team_id=team, member_ids=[m.id for m in members], provider=provider,
                  external_id=uid(), archived_at=now() if archived else None)
    db.add(group)
    db.flush()
    return group


def task(db, owner, group=None, provider=None):
    """A conversation, message and run; `provider` adds the inbound chat-platform event that started it."""
    conversation = Conversation(title='会话', owner_id=owner.id, group_id=group.id if group else None)
    db.add(conversation)
    db.flush()
    message = Message(conversation_id=conversation.id, role='user', content='hi')
    db.add(message)
    db.flush()
    run = Run(conversation_id=conversation.id, user_id=owner.id, message_id=message.id, status='succeeded')
    db.add(run)
    db.flush()
    if provider:
        db.add(IMEvent(provider=provider, event_id=uid(), run_id=run.id, reply_target={}))
        db.flush()
    return conversation, run


def entry(db, actor, action, target=None, details=None, **extra):
    row = Audit(actor_id=actor.id if actor else None, action=action, target_id=target, details={} if details is None else details, **extra)
    db.add(row)
    db.flush()
    return row


def context(db, row, who=None):
    return audit_context.describe(db, who or viewer(), [row])[row.id]


def test_a_group_chat_message_names_the_person_their_nickname_and_the_group(database):
    with database.begin() as db:
        zhang = person(db, '张三账号')
        known_as(db, zhang, 'feishu', 'ou_zhang', '张三')
        group = chat_group(db, '产品群', members=[zhang])
        _, run = task(db, zhang, group, 'feishu')
        rows = [entry(db, zhang, 'message.enqueue', 'message-id', {'run_id': run.id}),
                entry(db, zhang, 'run.succeeded', run.id),
                entry(db, zhang, 'platform.tool_call', 'feishu', {'tool': 'x', 'run_id': run.id})]
        got = [context(db, row) for row in rows]
    for item in got:
        assert item == {'actor_name': '张三账号', 'channel': 'feishu', 'nickname': '张三', 'private': False,
                        'group': {'name': '产品群', 'state': 'ok', 'archived': False}}


def test_a_private_chat_is_labelled_private_and_has_no_group(database):
    with database.begin() as db:
        li = person(db, '李四账号')
        known_as(db, li, 'dingtalk', 'staff-li', '李四')
        _, run = task(db, li, None, 'dingtalk')
        got = context(db, entry(db, li, 'run.failed', run.id, {'error': 'x'}))
    assert got == {'actor_name': '李四账号', 'channel': 'dingtalk', 'nickname': '李四', 'group': None, 'private': True}


def test_a_web_conversation_is_not_a_private_chat_and_has_no_nickname(database):
    with database.begin() as db:
        wang = person(db, '王五账号')
        known_as(db, wang, 'feishu', 'ou_wang', '王五')  # Has a Feishu name, but this task did not come from Feishu.
        _, run = task(db, wang, None, None)
        got = context(db, entry(db, wang, 'run.succeeded', run.id))
    assert got == {'actor_name': '王五账号', 'channel': 'web', 'nickname': None, 'group': None, 'private': False}


def test_the_nickname_is_the_one_for_the_channel_the_event_came_through(database):
    with database.begin() as db:
        both = person(db, '双平台')
        known_as(db, both, 'feishu', 'ou_both', '飞书名')
        known_as(db, both, 'dingtalk', 'staff-both', '钉钉名')
        _, via_feishu = task(db, both, None, 'feishu')
        _, via_dingtalk = task(db, both, None, 'dingtalk')
        names = [context(db, entry(db, both, 'run.succeeded', run.id))['nickname'] for run in (via_feishu, via_dingtalk)]
    assert names == ['飞书名', '钉钉名']


def test_the_nickname_belongs_to_the_actor_not_to_the_owner_of_the_task(database):
    with database.begin() as db:
        owner, operator = person(db, '任务主人'), person(db, '管理员', role='org_admin')
        known_as(db, owner, 'feishu', 'ou_owner', '主人昵称')
        _, run = task(db, owner, None, 'feishu')
        stopped = context(db, entry(db, operator, 'run.cancelled', run.id, {'source': 'feishu'}))
        known_as(db, operator, 'feishu', 'ou_op', '管理员昵称')
        again = context(db, entry(db, operator, 'run.cancelled', run.id))
    assert stopped['nickname'] is None and stopped['actor_name'] == '管理员' and stopped['channel'] == 'feishu'
    assert again['nickname'] == '管理员昵称'


def test_the_newest_known_nickname_wins_and_unresolved_ones_are_skipped(database):
    with database.begin() as db:
        user = person(db, '改名的人')
        known_as(db, user, 'feishu', 'ou_rename', '旧昵称', seen=now() - timedelta(days=3))
        db.add(IMDiscovery(provider='feishu', app_scope='scope', sender_id='ou_rename', chat_id='g', chat_type='group',
                           nickname='新昵称', reason='unbound', last_seen=now()))
        db.add(IMDiscovery(provider='feishu', app_scope='scope', sender_id='ou_rename', chat_id='h', chat_type='group',
                           nickname=None, reason='unbound', last_seen=now() + timedelta(hours=1)))
        _, run = task(db, user, None, 'feishu')
        got = context(db, entry(db, user, 'run.succeeded', run.id))
    assert got['nickname'] == '新昵称'


def test_a_person_without_a_resolved_nickname_still_gets_the_account_name_and_channel(database):
    with database.begin() as db:
        user = person(db, '无昵称')
        known_as(db, user, 'feishu', 'ou_none', None)
        _, run = task(db, user, None, 'feishu')
        got = context(db, entry(db, user, 'run.succeeded', run.id))
    assert got['nickname'] is None and got['actor_name'] == '无昵称' and got['channel'] == 'feishu'


def test_conversation_and_group_actions_point_at_their_group(database):
    with database.begin() as db:
        admin = person(db, '群管理', role='org_admin')
        member = person(db, '群成员')
        group = chat_group(db, '运营群', members=[member])
        conversation, _ = task(db, member, group, 'feishu')
        archived = entry(db, admin, 'conversation.archive', conversation.id, {'group_id': group.id, 'owner_id': member.id})
        approved = entry(db, admin, 'im.discovery.approve', 'discovery-id', {'group_id': group.id, 'user_id': member.id})
        bound = entry(db, admin, 'im.group.bind', group.id, {'discovery_id': 'd', 'member_ids': [member.id]})
        created = entry(db, admin, 'conversation.create', conversation.id)
        got = [context(db, row) for row in (archived, approved, bound, created)]
    for item in got:
        assert item['group'] == {'name': '运营群', 'state': 'ok', 'archived': False}
        assert item['actor_name'] == '群管理' and item['nickname'] is None


def test_a_platform_row_without_a_run_still_knows_its_channel(database):
    with database.begin() as db:
        user = person(db, '授权的人')
        known_as(db, user, 'dingtalk', 'staff-auth', '授权昵称')
        got = context(db, entry(db, user, 'platform.start', 'dingtalk', {'state': 'setup_required'}))
        other = context(db, entry(db, user, 'platform.start', 'elsewhere', {}))
    assert got['channel'] == 'dingtalk' and got['nickname'] == '授权昵称' and got['group'] is None
    assert other['channel'] is None and other['nickname'] is None


def test_a_group_the_viewer_cannot_read_is_not_named(database):
    with database.begin() as db:
        member = person(db, '成员')
        group = chat_group(db, '机密群', org='org', members=[member])
        _, run = task(db, member, group, 'feishu')
        row = entry(db, member, 'run.succeeded', run.id)
        everyone = {role: context(db, row, viewer(role, org='org', team='team')) for role in ('super_admin', 'org_admin', 'team_lead')}
        elsewhere = context(db, row, viewer('org_admin', org='other-org', team=None))
        other_team = context(db, row, viewer('team_lead', org='org', team='other-team'))
        inactive = viewer('super_admin')
        inactive.active = False
        locked_out = context(db, row, inactive)
    assert everyone['super_admin']['group'] == {'name': '机密群', 'state': 'ok', 'archived': False}
    assert everyone['org_admin']['group']['name'] == '机密群'
    for hidden in (elsewhere, other_team, locked_out):
        assert hidden['group'] == {'name': None, 'state': 'hidden', 'archived': False}
        assert hidden['channel'] == 'feishu'  # Only the group name is guarded.


def test_an_archived_group_is_still_named_within_the_viewers_scope_and_marked(database):
    with database.begin() as db:
        member = person(db, '成员')
        group = chat_group(db, '旧项目群', members=[member], archived=True)
        _, run = task(db, member, group, 'feishu')
        row = entry(db, member, 'run.succeeded', run.id)
        boss = context(db, row)
        manager = context(db, row, viewer('org_admin'))
        outsider = context(db, row, viewer('org_admin', org='other-org'))
        team_peer = context(db, row, viewer('team_lead', team='other-team'))
    assert boss['group'] == manager['group'] == {'name': '旧项目群', 'state': 'ok', 'archived': True}
    assert outsider['group']['state'] == team_peer['group']['state'] == 'hidden' and outsider['group']['name'] is None


def test_a_group_that_no_longer_exists_is_reported_as_missing(database):
    with database.begin() as db:
        admin = person(db, '管理员', role='org_admin')
        got = context(db, entry(db, admin, 'conversation.archive', 'gone', {'group_id': 'vanished-group'}))
    assert got['group'] == {'name': None, 'state': 'missing', 'archived': False}


@pytest.mark.parametrize('details', [None, [], 'text', 7, {'run_id': 5}, {'run_id': 'x' * 500}, {'run_id': ['a']}, {'group_id': {'a': 1}},
                                     {'group_id': ''}, {'run_id': None, 'group_id': None}])
def test_malformed_details_never_break_the_page(database, details):
    with database.begin() as db:
        user = person(db, '任何人')
        row = entry(db, user, 'message.enqueue', None, None)
        row.details = details
        got = context(db, row)
    assert got == {'actor_name': '任何人', 'channel': None, 'nickname': None, 'group': None, 'private': False}


def test_unknown_or_missing_actors_and_dangling_targets_are_tolerated(database):
    with database.begin() as db:
        system = entry(db, None, 'run.cancelled', 'no-such-run', {'source': 'operator'})
        ghost = Audit(actor_id='deleted-user', action='run.succeeded', target_id='no-such-run', details={})
        db.add(ghost)
        db.flush()
        got = [context(db, system), context(db, ghost)]
    assert got[0] == {'actor_name': None, 'channel': None, 'nickname': None, 'group': None, 'private': False}
    assert got[1]['actor_name'] is None and got[1]['channel'] is None


def test_an_empty_page_costs_no_queries(database):
    with database() as db:
        assert audit_context.describe(db, viewer(), []) == {}


def statements(factory, rows_for):
    counted = []
    with factory() as db:
        engine = db.get_bind()
        listener = lambda conn, cursor, statement, *_: counted.append(statement)  # noqa: E731
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            audit_context.describe(db, viewer(), rows_for(db))
        finally:
            event.remove(engine, 'before_cursor_execute', listener)
    return len(counted)


def test_the_number_of_queries_does_not_grow_with_the_number_of_rows(database):
    with database.begin() as db:
        for index in range(60):
            user = person(db, f'人{index}')
            known_as(db, user, 'feishu', f'ou_{index}', f'昵称{index}')
            group = chat_group(db, f'群{index}', members=[user])
            _, run = task(db, user, group, 'feishu')
            entry(db, user, 'run.succeeded', run.id)
    pick = lambda count: (lambda db: list(db.scalars(select(Audit).where(Audit.action == 'run.succeeded').limit(count))))  # noqa: E731
    small, large = statements(database, pick(2)), statements(database, pick(60))
    assert large == small and large <= 10


def test_both_audit_endpoints_keep_the_raw_ids_and_add_the_context(database):
    with database.begin() as db:
        zhang = person(db, '张三账号')
        known_as(db, zhang, 'feishu', 'ou_z', '张三')
        group = chat_group(db, '产品群', members=[zhang])
        _, run = task(db, zhang, group, 'feishu')
        entry(db, zhang, 'run.succeeded', run.id)
        actor_id = zhang.id
    with database() as db:
        page = main.audit_page(1, 50, viewer(), db)
        legacy = main.audit_events(viewer(), db)
    for items in (page['items'], legacy):
        row = next(item for item in items if item['action'] == 'run.succeeded')
        assert row['actor_id'] == actor_id and row['target_id'] == run.id
        assert row['context']['nickname'] == '张三' and row['context']['group']['name'] == '产品群'
        assert set(row) == {'id', 'actor_id', 'action', 'target_id', 'details', 'created_at', 'context'}
