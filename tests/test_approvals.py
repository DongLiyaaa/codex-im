"""Server-verified approvals: only the requester's own /approve releases a risky action, exactly as stored."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from test_im_postgres import database
from test_im_commands import send
from test_platform_auth import configure
from test_platform_workspace import (fake_cli, token_for, run_tools, body, executed, connect, approve, run_approved,
                                     client_for, DOC_URL)
from app import approvals, im, platform_bridge as bridge
from app.approvals import Decision
from app.models import Audit, Conversation, IMEvent, IMOutbox, Message, PlatformApproval, Run, User, now
from app.platform_workspace import WorkspaceError

DELETE = {'command': ['drive', '+delete'], 'flags': {'file-token': 'OTHER0001', 'type': 'docx'}, 'stdin': None, 'target_url': None}


def runs(database):
    with database() as db:
        return db.scalar(select(func.count()).select_from(Run))


def outbox(database):
    with database() as db:
        return [row.text for row in db.scalars(select(IMOutbox).order_by(IMOutbox.created_at))]


def state(database, code):
    with database() as db:
        return db.scalar(select(PlatformApproval).where(PlatformApproval.code == code)).state


def pending_in_im(database, args=DELETE, identity='user'):
    """A private IM conversation whose first turn finished and left one pending approval."""
    send(database, '帮我删掉那个文档')
    with database.begin() as db:
        run = db.scalar(select(Run))
        run.status = 'succeeded'
        row = approvals.request(db, db.get(User, run.user_id), run, 'feishu', 'command', identity, args)
        return row.code


@pytest.mark.parametrize('text,expected', [
    ('/approve K7M2QX', Decision('approve', 'K7M2QX')), ('/批准 k7m2qx', Decision('approve', 'K7M2QX')),
    ('@_user_1 /deny ABC234', Decision('deny', 'ABC234')), ('/approve', Decision('approve', None)),
    ('/确认', Decision('approve', None)), ('/拒绝 abc234', Decision('deny', 'ABC234')),
    ('approve ABC234', None), ('/approve ABC234 请执行', None), ('/approved ABC234', None), ('/' + 'x' * 60, None), ('', None)])
def test_parse(text, expected):
    assert approvals.parse(text) == expected


def test_request_is_idempotent_bounded_and_notifies_from_the_server(database):
    first = pending_in_im(database)
    with database.begin() as db:
        run = db.scalar(select(Run))
        user = db.get(User, run.user_id)
        assert approvals.request(db, user, run, 'feishu', 'command', 'user', DELETE).code == first  # No second code for a repeat.
        codes = {first}
        for i in range(approvals.MAX_PENDING - 1):
            codes.add(approvals.request(db, user, run, 'feishu', 'command', 'user', {**DELETE, 'flags': {'file-token': f'DOC{i}0000', 'type': 'docx'}}).code)
        assert len(codes) == approvals.MAX_PENDING
        with pytest.raises(WorkspaceError) as exc:
            approvals.request(db, user, run, 'feishu', 'command', 'user', {**DELETE, 'flags': {'file-token': 'EXTRA0001'}})
        assert exc.value.code == 'too_many_approvals'
        row = db.scalar(select(PlatformApproval).where(PlatformApproval.code == first))
        assert row.summary.startswith('飞书：以你本人身份执行高风险命令 drive +delete') and 'OTHER0001' in row.summary
        notices = [m.content for m in db.scalars(select(Message).where(Message.role == 'assistant'))]
        assert approvals.notice(row) in notices
        original = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id))
        clones = [e for e in db.scalars(select(IMEvent)) if e.run_id is None and e.reply_target == original.reply_target]
        assert len(clones) == approvals.MAX_PENDING  # One server notice per distinct request, same chat, same checks.
    assert all(f'/approve ' in text and '10 分钟' in text for text in outbox(database))


def test_only_the_requester_in_the_same_conversation_can_approve(database):
    code = pending_in_im(database)
    with database.begin() as db:
        run = db.scalar(select(Run))
        owner = db.get(User, run.user_id)
        conversation = db.get(Conversation, run.conversation_id)
        stranger = User(email='other@example.invalid', name='other', role='member', org_id=owner.org_id, team_id=owner.team_id,
                        active=True, password_hash='unused')
        db.add(stranger)
        db.flush()
        elsewhere = Conversation(owner_id=owner.id, title='web:elsewhere')
        db.add(elsewhere)
        db.flush()
        assert approvals.handle(db, stranger, conversation, Decision('approve', code)).reply == '没有权限处理这个会话里的操作。'
        assert approvals.handle(db, owner, elsewhere, Decision('approve', code)).reply == f'没有找到审批码 {code}。'
    assert state(database, code) == 'pending'


def test_approve_waits_while_a_task_runs_but_deny_always_works(database):
    code = pending_in_im(database)
    with database.begin() as db:
        run = db.scalar(select(Run))
        user, conversation = db.get(User, run.user_id), db.get(Conversation, run.conversation_id)
        assert approvals.handle(db, user, conversation, Decision('approve', code), busy=True) == approvals.Handled(approvals.BUSY, None)
    assert state(database, code) == 'pending'
    with database.begin() as db:
        handled = approvals.handle(db, user, conversation, Decision('deny', code), busy=True)
        assert '已拒绝' in handled.reply and handled.continuation is None
    with database() as db:
        row = db.scalar(select(PlatformApproval))
        assert row.state == 'denied' and row.payload == {}  # The stored request is dropped as soon as it is closed.


def test_expired_approval_cannot_be_approved_or_executed(database):
    code = pending_in_im(database)
    with database.begin() as db:
        db.scalar(select(PlatformApproval)).expires_at = now() - timedelta(seconds=1)
        run = db.scalar(select(Run))
        user, conversation = db.get(User, run.user_id), db.get(Conversation, run.conversation_id)
        assert '已过期' in approvals.handle(db, user, conversation, Decision('approve', code)).reply
    assert state(database, code) == 'expired'


def test_stored_request_is_what_runs_not_what_the_model_says(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    _, calls = fake_cli
    token, run_id, user_id = token_for(database, group=False)
    connect(database, user_id)
    content = '# 全新正文\n' + '很长的内容。' * 200
    request = body(run_tools(database, token, ('write_platform_resource', {
        'provider': 'feishu', 'kind': 'document', 'url': DOC_URL, 'content': content, 'mode': 'overwrite'}))[0])
    assert request['state'] == 'approval_required' and executed(calls) == []
    assert f'{len(content)} 字' in request['summary'] and content not in json.dumps(request, ensure_ascii=False)
    approve(database, run_id, user_id, request['approval_code'])
    done = run_approved(database, token, request['approval_code'])
    assert done['state'] == 'written' and done['kind'] == 'document' and done['provider'] == 'feishu'
    [update] = [c for c in calls() if c['args'][:2] == ['docs', '+update']]
    assert update['args'][update['args'].index('--command') + 1] == 'overwrite' and update['stdin'] == content
    with database() as db:
        row = db.scalar(select(PlatformApproval))
        assert row.state == 'consumed' and row.payload == {}


def test_approval_names_who_acts_and_a_different_identity_needs_a_new_one(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    token, run_id, user_id = token_for(database, group=False)
    connect(database, user_id)
    request = body(run_tools(database, token, ('run_platform_command', {
        'provider': 'feishu', 'command': ['drive', '+delete'], 'flags': {'file-token': 'OTHER0001', 'type': 'docx'}}))[0]) 
    code = request['approval_code']
    approve(database, run_id, user_id, code)
    with database.begin() as db:
        row = db.scalar(select(PlatformApproval))
        row.payload = {**row.payload, 'identity': 'bot'}  # Approved as the bot; execution would now be as the user.
    assert run_approved(database, token, code)['state'] == 'approval_identity_changed'
    assert executed(fake_cli[1]) == []


def test_approval_cannot_be_used_from_another_conversation(database, monkeypatch, fake_cli):
    configure(monkeypatch)
    token, run_id, user_id = token_for(database, group=False)
    connect(database, user_id)
    request = body(run_tools(database, token, ('run_platform_command', {
        'provider': 'feishu', 'command': ['drive', '+delete'], 'flags': {'file-token': 'OTHER0001', 'type': 'docx'}}))[0])
    approve(database, run_id, user_id, request['approval_code'])
    with database.begin() as db:  # A run of the same user, but in a different conversation.
        other = Conversation(owner_id=user_id, title='web:other')
        db.add(other)
        db.flush()
        message = Message(conversation_id=other.id, role='user', content='x')
        db.add(message)
        db.flush()
        run = Run(user_id=user_id, conversation_id=other.id, message_id=message.id, status='running')
        db.add(run)
        db.flush()
        other_token = bridge.issue(run)
    assert run_approved(database, other_token, request['approval_code'])['state'] == 'approval_not_found'
    assert executed(fake_cli[1]) == []


def test_im_approve_continues_with_a_normal_turn(database):
    code = pending_in_im(database)
    before = len(outbox(database))
    assert send(database, f'/approve {code}') == {'ok': True}
    assert state(database, code) == 'approved' and runs(database) == 2 and len(outbox(database)) == before
    with database() as db:
        last = db.scalars(select(Message).where(Message.role == 'user').order_by(Message.created_at.desc())).first()
        assert last.content.startswith(f'我批准了操作 {code}') and f'run_approved_platform_action(approval_id="{code}")' in last.content


def test_im_deny_is_answered_directly_without_a_model_turn(database):
    code = pending_in_im(database)
    assert send(database, f'/拒绝 {code}') == {'ok': True, 'approval': 'deny'}
    assert state(database, code) == 'denied' and runs(database) == 1
    assert outbox(database)[-1] == f'已拒绝（{code}），该操作不会执行。'
    assert send(database, f'/approve {code}')['approval'] == 'approve'
    assert outbox(database)[-1] == f'审批码 {code} 已经被拒绝。' and runs(database) == 1


def test_im_unknown_code_bare_command_and_ambiguity(database):
    code = pending_in_im(database)
    send(database, '/approve ZZZZZZ')
    assert outbox(database)[-1] == '没有找到审批码 ZZZZZZ。'
    with database.begin() as db:
        run = db.scalar(select(Run))
        second = approvals.request(db, db.get(User, run.user_id), run, 'feishu', 'command', 'user', {**DELETE, 'flags': {'file-token': 'SECOND001', 'type': 'docx'}}).code
    send(database, '/approve')
    assert code in outbox(database)[-1] and second in outbox(database)[-1] and outbox(database)[-1].startswith('有多个待确认的操作')
    send(database, f'/deny {second}')
    assert send(database, '/approve') == {'ok': True}  # Exactly one left: no code needed.
    assert state(database, code) == 'approved'


def test_im_approve_while_busy_keeps_the_approval_pending(database):
    send(database, '帮我删掉那个文档')  # Leaves a queued run: the conversation is busy.
    with database.begin() as db:
        run = db.scalar(select(Run))
        code = approvals.request(db, db.get(User, run.user_id), run, 'feishu', 'command', 'user', DELETE).code
    assert send(database, f'/approve {code}') == {'ok': True, 'approval': 'approve'}
    assert outbox(database)[-1] == approvals.BUSY and state(database, code) == 'pending' and runs(database) == 1


def test_status_lists_pending_approvals(database):
    code = pending_in_im(database)
    send(database, '/status')
    assert f'待确认：{code}' in outbox(database)[-1] and f'/approve {code}' in outbox(database)[-1]


def web_conversation(database):
    with database.begin() as db:
        user = db.scalar(select(User))
        conversation = Conversation(owner_id=user.id, title='web chat')
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, role='user', content='删掉那个文档')
        db.add(message)
        db.flush()
        run = Run(user_id=user.id, conversation_id=conversation.id, message_id=message.id, status='succeeded')
        db.add(run)
        db.flush()
        code = approvals.request(db, user, run, 'feishu', 'command', 'user', DELETE).code
        return user, conversation.id, code


@pytest.fixture
def web(database):
    client, main = client_for(database)
    user, conversation_id, code = web_conversation(database)
    main.app.dependency_overrides[main.current_user] = lambda: user
    yield client, conversation_id, code
    main.app.dependency_overrides.clear()


def test_web_notice_is_a_server_message_in_the_conversation(database, web):
    _, conversation_id, code = web
    with database() as db:
        notices = [m.content for m in db.scalars(select(Message).where(Message.conversation_id == conversation_id, Message.role == 'assistant'))]
        assert len(notices) == 1 and code in notices[0] and 'drive +delete' in notices[0]
    assert outbox(database) == []  # Web runs have no chat to notify.


def test_web_approve_runs_a_model_turn(database, web):
    client, conversation_id, code = web
    response = client.post(f'/api/conversations/{conversation_id}/messages', json={'content': f'/approve {code}'})
    assert response.status_code == 202 and response.json()['run']['status'] == 'queued'
    assert response.json()['user_message']['content'].startswith(f'我批准了操作 {code}')
    assert state(database, code) == 'approved'


def test_web_deny_and_unknown_code_are_recorded_without_a_run(database, web):
    client, conversation_id, code = web
    denied = client.post(f'/api/conversations/{conversation_id}/messages', json={'content': f'/deny {code}'})
    assert denied.status_code == 202 and denied.json()['run'] is None
    unknown = client.post(f'/api/conversations/{conversation_id}/messages', json={'content': '/approve ZZZZZZ'})
    assert unknown.status_code == 202 and unknown.json()['run'] is None
    with database() as db:
        texts = [m.content for m in db.scalars(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id))]
        assert f'已拒绝（{code}），该操作不会执行。' in texts and '没有找到审批码 ZZZZZZ。' in texts
    assert state(database, code) == 'denied' and runs(database) == 1
