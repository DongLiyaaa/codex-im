"""Claude CLI as an additional agent: per-conversation binding, switching, payloads and failure reasons."""
import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_im_commands import wired, send, count
from test_im_postgres import database  # noqa: F401  (fixture used by wired)
from app import agents, im_commands, main, schemas, service
from app.models import Conversation, Group, IMOutbox, Message, Run, User


@pytest.fixture
def both(monkeypatch):
    monkeypatch.setenv('HUB_AGENTS', 'codex,claude')
    monkeypatch.delenv('DEFAULT_AGENT', raising=False)


def say(wired_fixture, text, **kw):
    database, sent = wired_fixture
    result = send(database, text, **kw)
    im_commands.flush()
    return result, sent[-1][1] if sent else None


def finish_current_run(database):
    with database.begin() as db:
        for run in db.scalars(select(Run)):
            run.status = 'succeeded'
        from app.models import IMReaction
        for reaction in db.scalars(select(IMReaction)):
            reaction.state = 'cleared'


def test_codex_is_the_default_and_claude_is_opt_in(monkeypatch):
    monkeypatch.delenv('HUB_AGENTS', raising=False)
    monkeypatch.delenv('DEFAULT_AGENT', raising=False)
    assert agents.enabled() == ['codex'] and agents.default() == 'codex'
    monkeypatch.setenv('HUB_AGENTS', 'codex, Claude')
    assert agents.enabled() == ['codex', 'claude']
    monkeypatch.setenv('HUB_AGENTS', 'nonsense')
    assert agents.enabled() == ['codex']
    monkeypatch.setenv('HUB_AGENTS', 'claude')
    assert agents.enabled() == ['claude'] and agents.default() == 'claude'


def test_resolve_prefers_the_persons_choice_only_while_enabled(monkeypatch, both):
    user = User(preferred_agent='claude')
    assert agents.resolve(user) == 'claude'
    monkeypatch.setenv('HUB_AGENTS', 'codex')
    assert agents.resolve(user) == 'codex'
    assert agents.resolve(User(preferred_agent=None)) == 'codex'


def test_codex_conversation_and_payload_are_unchanged(wired, monkeypatch):
    database, _ = wired
    monkeypatch.delenv('HUB_AGENTS', raising=False)
    assert send(database, '你好') == {'ok': True}
    with database() as db:
        run = db.scalar(select(Run))
        assert run.agent == 'codex' and db.get(Conversation, run.conversation_id).agent == 'codex'
        assert 'agent' not in service.build_payload(db, run)


def test_agent_command_reports_and_validates(wired, both):
    database, sent = wired
    _, text = say(wired, '/agent')
    assert 'Codex CLI' in text and '/agent claude' in text
    _, text = say(wired, '/agent gemini')
    assert '没有这个 Agent' in text
    _, text = say(wired, '/agent codex')
    assert '已经在使用 Codex CLI' in text
    assert count(database, Run) == 0 and count(database, Conversation, Conversation.archived_at.is_not(None)) == 0


def test_agent_command_refuses_a_disabled_agent(wired, monkeypatch):
    database, _ = wired
    monkeypatch.delenv('HUB_AGENTS', raising=False)
    _, text = say(wired, '/agent claude')
    assert '尚未在 Hub 启用' in text
    send(database, '你好')
    with database() as db:
        assert db.scalar(select(Run)).agent == 'codex'


def test_switching_starts_a_clean_conversation_bound_to_claude(wired, both):
    database, sent = wired
    send(database, '第一句，用 Codex')
    finish_current_run(database)
    with database() as db:
        old = db.scalar(select(Conversation)).id
    _, text = say(wired, '/agent claude')
    assert '已切换到 Claude CLI' in text
    with database() as db:
        assert db.get(Conversation, old).archived_at is not None
        assert db.scalar(select(User)).preferred_agent == 'claude'
    assert send(database, '第二句，用 Claude') == {'ok': True}
    with database() as db:
        run = db.scalars(select(Run).order_by(Run.created_at.desc())).first()
        conversation = db.get(Conversation, run.conversation_id)
        assert run.conversation_id != old and conversation.agent == 'claude' and run.agent == 'claude'
        payload = service.build_payload(db, run)
        assert payload['agent'] == 'claude'
        assert '第一句' not in payload['prompt'] and '第二句' in payload['prompt']


def test_new_keeps_the_agent_of_the_conversation(wired, both):
    database, _ = wired
    say(wired, '/agent claude')
    send(database, '你好')
    finish_current_run(database)
    say(wired, '/new')
    with database() as db:
        live = list(db.scalars(select(Conversation).where(Conversation.archived_at.is_(None))))
        assert [c.agent for c in live] == ['claude']


def test_preference_decides_the_agent_of_a_fresh_conversation(wired, both):
    database, _ = wired
    with database.begin() as db:
        db.scalar(select(User)).preferred_agent = 'claude'
    send(database, '你好')
    with database() as db:
        assert db.scalar(select(Run)).agent == 'claude'


def test_group_member_cannot_switch_the_groups_agent(wired, both):
    database, sent = wired
    with database.begin() as db:
        group = db.scalar(select(Group))
        other = User(email='o@example.invalid', name='other', role='member', org_id='org', team_id='team', active=True, password_hash='x')
        db.add(other); db.flush()
        db.add(Conversation(title='x', owner_id=other.id, group_id=group.id))
    send(database, '大家好', group=True)
    _, text = say(wired, '/agent claude', group=True)
    assert '群管理员' in text
    with database() as db:
        assert {c.agent for c in db.scalars(select(Conversation))} == {'codex'}
        assert db.scalar(select(User).where(User.name == '测试成员')).preferred_agent is None


def test_conversation_on_a_disabled_agent_gets_a_notice_and_no_run(wired, both, monkeypatch):
    database, sent = wired
    say(wired, '/agent claude')
    monkeypatch.setenv('HUB_AGENTS', 'codex')
    result = send(database, '你好')
    assert result == {'ok': True, 'agent_disabled': True} and count(database, Run) == 0
    im_commands.flush()
    assert 'Claude CLI 当前未启用' in sent[-1][1] and '/agent codex' in sent[-1][1]
    _, text = say(wired, '/agent codex')
    assert '已切换到 Codex CLI' in text


def test_api_creates_conversations_for_the_chosen_agent(wired, both):
    database, _ = wired
    with database() as db:
        actor = db.scalar(select(User))
    with database.begin() as db:
        assert main.list_agents(actor) == {'agents': [{'id': 'codex', 'label': 'Codex CLI'}, {'id': 'claude', 'label': 'Claude CLI'}],
                                           'default': 'codex', 'preferred': 'codex'}
        assert main.set_agent_preference(schemas.AgentPreference(agent='claude'), actor, db) == {'preferred': 'claude'}
    with database.begin() as db:
        fresh_actor = db.get(User, actor.id)
        assert main.create_conversation(schemas.ConversationCreate(title='a'), fresh_actor, db).agent == 'claude'
        assert main.create_conversation(schemas.ConversationCreate(title='b', agent='codex'), fresh_actor, db).agent == 'codex'
    with pytest.raises(HTTPException) as refused, database.begin() as db:
        agents.require_enabled('gemini')
    assert refused.value.status_code == 422


def test_api_refuses_an_agent_that_is_not_enabled(wired, monkeypatch):
    database, _ = wired
    monkeypatch.delenv('HUB_AGENTS', raising=False)
    with database() as db:
        actor = db.scalar(select(User))
    with pytest.raises(HTTPException) as refused, database.begin() as db:
        main.create_conversation(schemas.ConversationCreate(title='a', agent='claude'), actor, db)
    assert refused.value.status_code == 422 and '未启用' in refused.value.detail


def run_with_reply(wired_fixture, monkeypatch, agent, response):
    database, _ = wired_fixture
    with database.begin() as db:
        db.scalar(select(User)).preferred_agent = agent
    send(database, '你好')
    with database.begin() as db:
        run = db.scalar(select(Run))
        run.status, run_id = 'running', run.id
    monkeypatch.setenv('RUNNER_TOKEN', 'x' * 40)
    monkeypatch.delenv('PLATFORM_BRIDGE_KEY', raising=False)
    seen, delivered = [], []
    monkeypatch.setattr(service, 'deliver', lambda rid, text: delivered.append(text))

    def runner(request):
        seen.append(request.read())
        return response
    real = httpx.Client
    monkeypatch.setattr(service.httpx, 'Client', lambda **kw: real(transport=httpx.MockTransport(runner), **kw))
    service.execute_run(run_id)
    with database() as db:
        return db.get(Run, run_id), seen, delivered


def test_claude_run_is_sent_to_the_runner_with_its_agent(wired, both, monkeypatch):
    run, seen, delivered = run_with_reply(wired, monkeypatch, 'claude', httpx.Response(200, json={'text': 'Claude 的回答'}))
    assert run.status == 'succeeded' and b'"agent":"claude"' in seen[0].replace(b' ', b'')
    assert delivered == ['Claude 的回答']


def test_codex_run_body_has_no_agent_field(wired, both, monkeypatch):
    run, seen, _ = run_with_reply(wired, monkeypatch, 'codex', httpx.Response(200, json={'text': 'ok'}))
    assert run.status == 'succeeded' and b'"agent"' not in seen[0]


@pytest.mark.parametrize('code,reason', [('CLAUDE_API_KEY_NOT_CONFIGURED', 'Claude CLI 未配置 API Key'),
                                         ('CLAUDE_MCP_UNAVAILABLE', '所需工具连接失败'),
                                         ('RUN_TIMEOUT', '执行超时')])
def test_claude_failures_get_a_static_reason(wired, both, monkeypatch, code, reason):
    run, _, delivered = run_with_reply(wired, monkeypatch, 'claude', httpx.Response(503, json={'detail': code}))
    assert run.status == 'failed' and run.error == reason
    assert delivered == [f'本次处理失败：{reason}。请稍后重新发送；如持续失败请联系管理员。']


def test_codex_failures_keep_the_generic_reason(wired, both, monkeypatch):
    run, _, _ = run_with_reply(wired, monkeypatch, 'codex', httpx.Response(504, json={'detail': 'RUN_TIMEOUT'}))
    assert run.error == 'Runner execution failed; check runner configuration and logs'


def test_runner_without_the_agent_enabled_is_reported(wired, both, monkeypatch):
    run, _, _ = run_with_reply(wired, monkeypatch, 'claude', httpx.Response(422, json={'detail': 'AGENT_NOT_ENABLED'}))
    assert run.error == '所选 Agent 在执行服务中未启用'
