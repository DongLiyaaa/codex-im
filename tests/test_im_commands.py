"""IM slash commands, outbox delivery, cancellation and failure notices: real isolated PostgreSQL."""
import json
from datetime import datetime, timezone
import httpx
import pytest
from sqlalchemy import select, func
from test_im_postgres import database
from app import im, im_commands, im_reactions, service
from app.models import Conversation, Group, IMEvent, IMOutbox, IMReaction, Message, Run, User
from app.models import uid as new_id


@pytest.fixture
def wired(database, monkeypatch):
    monkeypatch.setattr(service, 'SessionLocal', database)
    monkeypatch.setattr(im_commands, 'SessionLocal', database)
    monkeypatch.setattr(im_reactions, 'process', lambda *a, **k: None)
    sent = []
    def fake_deliver(db, event, text):
        sent.append((event.reply_target.get('conversation_id'), text))
        event.delivered_at = datetime.now(timezone.utc)
    monkeypatch.setattr(im, 'deliver_event', fake_deliver)
    return database, sent


def send(database, text, group=False, sender='sender'):
    with database.begin() as db:
        return im._enqueue(db, 'feishu', new_id(), sender, 'chat' if group else 'private-chat', text, group)


def count(database, model, *where):
    with database() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


@pytest.mark.parametrize('text,command', [('/help', 'help'), ('@_user_1 /new', 'new'), ('/STATUS', 'status'),
                                          ('  /停止 ', 'stop'), ('/reset', 'new'), ('/unknown', None), ('hello /new', None),
                                          ('/' + 'x' * 70, None)])
def test_parse(text, command):
    assert im_commands.parse(text) == command


def test_help_and_status_never_create_runs_and_reply_via_outbox(wired):
    database, sent = wired
    assert send(database, '/help') == {'ok': True, 'command': 'help'}
    assert send(database, '/status', group=True) == {'ok': True, 'command': 'status'}
    assert count(database, Run) == 0 and count(database, IMReaction) == 0 and count(database, Message) == 0
    assert count(database, IMOutbox, IMOutbox.state == 'pending') == 2
    assert im_commands.flush() == 2
    assert count(database, IMOutbox, IMOutbox.state == 'sent') == 2
    assert '/new' in sent[0][1] and '飞书文档' in sent[0][1]
    assert '群聊「测试群」' in sent[1][1] and '当前任务：无' in sent[1][1]


def test_stop_cancels_own_runs_and_discards_running_result(wired, monkeypatch):
    database, sent = wired
    assert send(database, '写一段总结') == {'ok': True}
    with database.begin() as db:
        run = db.scalar(select(Run))
        run.status, run_id = 'running', run.id
    monkeypatch.setenv('RUNNER_TOKEN', 'x' * 40)
    monkeypatch.delenv('PLATFORM_BRIDGE_KEY', raising=False)
    delivered = []
    monkeypatch.setattr(service, 'deliver', lambda rid, text: delivered.append(text))
    def runner(request):
        # /stop arrives while Codex is still working on the run.
        assert send(database, '/stop') == {'ok': True, 'command': 'stop'}
        return httpx.Response(200, json={'text': 'late answer'})
    real = httpx.Client
    monkeypatch.setattr(service.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(runner), **kw))
    service.execute_run(run_id)
    with database() as db:
        assert db.get(Run, run_id).status == 'cancelled'
        assert [m.role for m in db.scalars(select(Message))] == ['user']
    assert delivered == []
    im_commands.flush()
    assert '已停止 1 个任务' in sent[-1][1]


def test_group_member_cannot_stop_or_reset_others(wired):
    database, sent = wired
    with database.begin() as db:
        group = db.scalar(select(Group))
        other = User(email='o@example.invalid', name='other', role='member', org_id='org', team_id='team', active=True, password_hash='x')
        db.add(other); db.flush()
        conversation = Conversation(title='x', owner_id=other.id, group_id=group.id)
        db.add(conversation); db.flush()
    assert send(database, '大家好', group=True) == {'ok': True}
    with database.begin() as db:
        run = db.scalar(select(Run))
        run.user_id = db.scalar(select(User).where(User.name == 'other')).id
    send(database, '/stop', group=True)
    send(database, '/new', group=True)
    im_commands.flush()
    assert '没有你可以停止的任务' in sent[0][1]
    assert '/stop' in sent[1][1] or '群管理员' in sent[1][1]
    with database() as db:
        assert db.scalar(select(Run)).status == 'queued'
        assert db.scalar(select(func.count()).select_from(Conversation).where(Conversation.archived_at.is_not(None))) == 0


def test_new_rotates_private_conversation(wired):
    database, sent = wired
    assert send(database, '第一句') == {'ok': True}
    with database.begin() as db:
        first = db.scalar(select(Run))
        first.status = 'succeeded'
        old_conversation = first.conversation_id
        db.scalar(select(IMReaction)).state = 'cleared'  # Done by reaction cleanup after a delivered reply.
    assert send(database, '/new') == {'ok': True, 'command': 'new'}
    with database() as db:
        assert db.get(Conversation, old_conversation).archived_at is not None
    im_commands.flush()
    new_conversation, text = sent[-1]
    assert new_conversation != old_conversation and '已开启新会话' in text
    assert send(database, '第二句') == {'ok': True}
    with database() as db:
        latest = db.scalars(select(Run).order_by(Run.created_at.desc())).first()
        assert latest.conversation_id == new_conversation


def test_new_refused_while_task_active(wired):
    database, sent = wired
    send(database, '第一句')
    send(database, '/new')
    im_commands.flush()
    assert '/stop' in sent[-1][1]
    assert count(database, Conversation, Conversation.archived_at.is_not(None)) == 0


def test_unauthorized_sender_command_is_ignored(wired):
    database, sent = wired
    assert send(database, '/status', sender='stranger')['pending']
    assert count(database, IMOutbox) == 0


def test_outbox_failure_and_crash_recovery_never_replays(wired, monkeypatch):
    database, sent = wired
    send(database, '/help')
    send(database, '/help')
    monkeypatch.setattr(im, 'deliver_event', lambda db, event, text: setattr(event, 'delivery_error', 'IM_DELIVERY_FAILED'))
    assert im_commands.flush(limit=1) == 1
    with database.begin() as db:
        pending = db.scalar(select(IMOutbox).where(IMOutbox.state == 'pending'))
        pending.state = 'sending'
    im_commands.recover()
    with database() as db:
        assert sorted(r.state for r in db.scalars(select(IMOutbox))) == ['ambiguous', 'failed']
    assert im_commands.flush() == 0


def test_failure_notice_is_sent_without_upstream_detail(wired, monkeypatch):
    database, _ = wired
    send(database, '你好')
    with database.begin() as db:
        run = db.scalar(select(Run))
        run.status, run_id = 'running', run.id
    monkeypatch.delenv('RUNNER_TOKEN', raising=False)
    delivered = []
    monkeypatch.setattr(service, 'deliver', lambda rid, text: delivered.append(text))
    service.execute_run(run_id)
    # Internal ASCII error strings are never echoed to IM users.
    assert delivered == ['本次处理失败：处理异常。请稍后重新发送；如持续失败请联系管理员。']
    assert service.failure_notice('Runner execution failed; check runner configuration and logs') == \
        '本次处理失败：执行服务暂时不可用。请稍后重新发送；如持续失败请联系管理员。'
