"""Offline tests. Install pytest==8.3.5 httpx==0.28.1 alongside requirements."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tomllib

import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("hub_runner", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

probe_spec = importlib.util.spec_from_file_location("hub_sandbox_probe", Path(__file__).with_name("sandbox_probe.py"))
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("RUNNER_TOKEN", "t" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "secret-model-key")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_AUTH_MODE", raising=False)
    monkeypatch.delenv("CODEX_OAUTH_AUTH_FILE", raising=False)
    monkeypatch.delenv("CODEX_PROXY_URL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_BASE_URL", raising=False)
    m.app.state.active = 0


def payload(**kwargs):
    return m.Execute(run_id="r", conversation_id="c", prompt="hello", **kwargs)


def test_fixed_platform_bridge_keeps_private_mcp_forbidden(tmp_path, monkeypatch):
    monkeypatch.setenv('PLATFORM_BRIDGE_URL', 'http://127.0.0.1:18200/internal/platform-mcp')
    capability = 'test.' + 'a' * 64
    _, env = m.prepare(tmp_path, payload(platform_capability=capability), 'key')
    config = tomllib.loads((Path(env['CODEX_HOME']) / 'config.toml').read_text())
    assert config['mcp_servers']['hub_personal_platforms']['url'] == 'http://127.0.0.1:18200/internal/platform-mcp'
    assert capability not in config['developer_instructions']
    assert 'PLATFORM_BRIDGE_KEY' not in env
    with pytest.raises(ValueError):
        m.MCP(name='private', url='http://127.0.0.1:18200/internal/platform-mcp')
    with pytest.raises(ValueError):
        m.Execute(run_id='r', conversation_id='c', prompt='x', platform_url='http://other/')


def test_optional_proxy_reaches_codex_but_not_local_bridges(tmp_path, monkeypatch):
    (tmp_path / 'none').mkdir(); (tmp_path / 'proxied').mkdir()
    _, env = m.prepare(tmp_path / 'none', payload(), 'key')
    assert not any('proxy' in k.lower() for k in env)
    monkeypatch.setenv('CODEX_PROXY_URL', 'http://127.0.0.1:7890')
    _, env = m.prepare(tmp_path / 'proxied', payload(), 'key')
    assert env['HTTPS_PROXY'] == env['http_proxy'] == 'http://127.0.0.1:7890'
    assert '127.0.0.1' in env['NO_PROXY'].split(',') and 'localhost' in env['no_proxy'].split(',')
    for bad in ('127.0.0.1:7890', 'ftp://127.0.0.1:7890', 'http://127.0.0.1:7890/path'):
        monkeypatch.setenv('CODEX_PROXY_URL', bad)
        with pytest.raises(RuntimeError):
            m.proxy_env()


def test_proxy_bypass_includes_the_service_hosts_of_the_internal_bridges(monkeypatch):
    monkeypatch.setenv('CODEX_PROXY_URL', 'http://host.docker.internal:7890')
    monkeypatch.setenv('PLATFORM_BRIDGE_URL', 'http://api:18200/internal/platform-mcp')
    monkeypatch.setenv('ATTACHMENT_BRIDGE_URL', 'http://API:18200/internal/attachment-mcp')
    bypass = m.proxy_env()['NO_PROXY'].split(',')
    assert bypass == ['127.0.0.1', 'localhost', '::1', 'api']  # Once, lower-cased, never the model proxy itself.
    monkeypatch.setenv('PLATFORM_BRIDGE_URL', 'not a url')
    monkeypatch.setenv('ATTACHMENT_BRIDGE_URL', '')
    assert m.proxy_env()['NO_PROXY'] == '127.0.0.1,localhost,::1'


def written_config(tmp_path, key="secret-model-key"):
    _, env = m.prepare(tmp_path, payload(), key)
    path = Path(env["CODEX_HOME"]) / "config.toml"
    return tomllib.loads(path.read_text()), path.read_text(), env


def test_default_has_no_model_or_provider_override(tmp_path):
    config, _, _ = written_config(tmp_path)
    assert "model" not in config and "model_provider" not in config and "model_providers" not in config


def test_custom_model_and_endpoint_reach_codex_without_the_key_in_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("CODEX_BASE_URL", "https://models.example.com/v1/")
    config, text, env = written_config(tmp_path)
    assert config["model"] == "gpt-6.1-sol" and config["model_provider"] == "hub_model"
    assert config["model_providers"]["hub_model"] == {
        "name": "Hub model endpoint", "base_url": "https://models.example.com/v1", "env_key": "CODEX_API_KEY", "wire_api": "responses"}
    assert env["CODEX_API_KEY"] == "secret-model-key" and "secret-model-key" not in text
    assert config["forced_login_method"] == "api" and config["sandbox_mode"] == "read-only"  # Isolation settings stay in force.


def test_model_alone_keeps_the_default_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.1-sol")
    config, _, _ = written_config(tmp_path)
    assert config["model"] == "gpt-6.1-sol" and "model_provider" not in config and "model_providers" not in config


@pytest.mark.parametrize("url", [
    "http://models.example.com/v1", "ftp://models.example.com/v1", "models.example.com/v1", "https://",
    "https://user:pass@models.example.com/v1", "https://models.example.com/v1?key=1", "https://models.example.com/v1#x",
    "https://models.example.com:8443/v1", "https://models.example.com/v1 /x", "https://models.example.com/v1/../x?",
    "https://localhost/v1", "https://LOCALHOST./v1", "https://127.0.0.1/v1", "https://[::1]/v1", "https://10.0.0.5/v1",
    "https://192.168.1.10/v1", "https://169.254.169.254/latest", "https://172.16.0.1/v1", "https://100.64.0.1/v1",
    "https://[::ffff:127.0.0.1]/v1", "https://[fd00::1]/v1", "https://db/v1", "https://api.internal/v1", "https://printer.local/v1",
    "https://host.localhost/v1", "https://models.example.com:abc/v1",
])
def test_unsafe_endpoints_are_refused_everywhere(tmp_path, monkeypatch, url):
    monkeypatch.setenv("CODEX_BASE_URL", url)
    with pytest.raises(ValueError, match="INVALID_CODEX_BASE_URL"):
        m.model_settings()
    assert m.configured() == (None, "INVALID_CODEX_BASE_URL")  # Visible in /health, and every /execute is refused.
    with TestClient(m.app) as client:
        assert client.get("/health").json()["error"] == "INVALID_CODEX_BASE_URL"


def test_public_ip_literals_and_plain_names_are_accepted(monkeypatch):
    for url in ("https://8.8.8.8/v1", "https://models.example.com", "https://a.b.example.org/openai/v1"):
        monkeypatch.setenv("CODEX_BASE_URL", url)
        assert m.model_settings() == (None, url)


@pytest.mark.parametrize("model", ["bad model", "gpt;rm", "../x", "-x", "x" * 101, "a\nb", "gpt-6.1-sol\"", "é"])
def test_invalid_model_ids_are_refused(monkeypatch, model):
    monkeypatch.setenv("CODEX_MODEL", model)
    assert m.configured() == (None, "INVALID_CODEX_MODEL")


def test_a_chatgpt_login_is_never_pointed_at_a_custom_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_AUTH_MODE", "chatgpt")
    monkeypatch.setenv("CODEX_BASE_URL", "https://models.example.com/v1")
    assert m.configured() == (None, "CODEX_BASE_URL_REQUIRES_API_MODE")
    with TestClient(m.app) as client:
        assert client.get("/health").json()["error"] == "CODEX_BASE_URL_REQUIRES_API_MODE"
    monkeypatch.delenv("CODEX_BASE_URL")
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.1-sol")  # A model name alone is harmless in ChatGPT mode.
    assert m.configured()[1] == "OAUTH_SOURCE_NOT_CONFIGURED"


def test_custom_endpoint_does_not_change_proxy_or_bridge_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BASE_URL", "https://models.example.com/v1")
    monkeypatch.setenv("CODEX_PROXY_URL", "http://host.docker.internal:7890")
    monkeypatch.setenv("PLATFORM_BRIDGE_URL", "http://api:18200/internal/platform-mcp")
    _, _, env = written_config(tmp_path)
    assert env["HTTPS_PROXY"] == "http://host.docker.internal:7890" and "api" in env["NO_PROXY"].split(",")


def test_auth_and_validation_do_not_echo_secrets():
    with TestClient(m.app) as client:
        assert client.post("/execute", json={}).status_code == 401
        r = client.post("/execute", headers={"Authorization": "Bearer " + "t" * 40},
                        json={"secret": "do-not-echo"})
        assert r.status_code == 422
        assert "do-not-echo" not in r.text


def test_health_and_missing_credentials(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    with TestClient(m.app) as client:
        assert client.get("/health").json()["error"] == "MODEL_API_KEY_NOT_CONFIGURED"
        assert client.post("/execute", headers={"Authorization": "Bearer " + "t" * 40},
                           json=payload().model_dump()).status_code == 503


@pytest.fixture
def fresh_sandbox_cache():
    m._sandbox_health.update(at=-1e9, error=None)
    yield
    m._sandbox_health.update(at=-1e9, error=None)


def linux_runner(monkeypatch, outcome):
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(m.sys, "platform", "linux")
    calls = []

    async def fake_process(argv, cwd, env, stdin=b"", **kwargs):
        calls.append(argv)
        if outcome == "fail":
            raise m.RunnerError("CODEX_EXECUTION_FAILED")
        if outcome == "hang":
            await asyncio.sleep(60)
    monkeypatch.setattr(m, "process", fake_process)
    return calls


def test_health_is_not_ready_while_the_sandbox_cannot_start(monkeypatch, fresh_sandbox_cache):
    calls = linux_runner(monkeypatch, "fail")
    with TestClient(m.app) as client:
        body = client.get("/health").json()
    assert body["status"] == "not_ready" and body["error"] == "SANDBOX_UNAVAILABLE"
    assert calls and calls[0][1] == "sandbox" and calls[0][-1] == str(m.SANDBOX_PROBE)  # Never contacts a model.


def test_health_is_ready_when_the_sandbox_works_and_the_check_is_cached(monkeypatch, fresh_sandbox_cache):
    calls = linux_runner(monkeypatch, "ok")
    with TestClient(m.app) as client:
        assert [client.get("/health").json()["status"] for _ in range(3)] == ["ready"] * 3
    assert len(calls) == 1  # One preflight serves the whole cache window.


def test_health_rechecks_after_the_cache_expires(monkeypatch, fresh_sandbox_cache):
    calls = linux_runner(monkeypatch, "ok")
    with TestClient(m.app) as client:
        client.get("/health")
        m._sandbox_health["at"] -= m.SANDBOX_HEALTH_TTL + 1
        client.get("/health")
    assert len(calls) == 2


def test_a_hanging_sandbox_check_is_reported_not_waited_for_forever(monkeypatch, fresh_sandbox_cache):
    import time as clock
    linux_runner(monkeypatch, "hang")
    real_timeout = asyncio.timeout
    monkeypatch.setattr(m.asyncio, "timeout", lambda seconds: real_timeout(0.2))
    started = clock.monotonic()
    with TestClient(m.app) as client:
        assert client.get("/health").json()["error"] == "SANDBOX_UNAVAILABLE"
    assert clock.monotonic() - started < 5


def test_missing_credentials_are_reported_before_any_sandbox_check(monkeypatch, fresh_sandbox_cache):
    calls = linux_runner(monkeypatch, "ok")
    monkeypatch.delenv("OPENAI_API_KEY")
    with TestClient(m.app) as client:
        assert client.get("/health").json()["error"] == "MODEL_API_KEY_NOT_CONFIGURED"
    assert calls == []


def test_isolation_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "must-not-inherit")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid")
    p = payload(skills=[{"name": "assigned", "content": "approved content"}],
                mcps=[{"name": "remote", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer private"}}])
    roots = [tmp_path / "one", tmp_path / "two"]
    homes = []
    for root in roots:
        root.mkdir()
        cwd, env = m.prepare(root, p, "secret-model-key")
        homes.append(env["CODEX_HOME"])
        assert "DATABASE_URL" not in env and "OPENAI_BASE_URL" not in env
        assert env["CODEX_API_KEY"] == "secret-model-key"
        config = tomllib.loads((Path(env["CODEX_HOME"]) / "config.toml").read_text())
        assert config["sandbox_mode"] == "read-only"
        assert config["approval_policy"] == "never"
        assert config["features"]["shell_tool"] is False
        assert config["features"]["skip_host_skill_discovery"] is True
        assert config["skills"]["include_instructions"] is False
        assert "approved content" in config["developer_instructions"]
        assert "not a security boundary" in config["developer_instructions"]
        assert str(roots[1 if root == roots[0] else 0]) not in config["developer_instructions"]
        assert config["mcp_servers"]["remote"]["http_headers"]["Authorization"] == "Bearer private"
        assert (cwd / ".agents/skills/assigned/SKILL.md").read_text() == "approved content"
    assert homes[0] != homes[1]


@pytest.mark.parametrize("url", ["http://example.com", "file:///tmp/key", "https://user:pass@example.com", "https://example.com:8443", "https://example.com/#secret"])
def test_invalid_mcp(url):
    with pytest.raises(ValueError):
        m.MCP(name="m", url=url)


def test_paths_and_duplicate_names():
    with pytest.raises(ValueError):
        payload(skills=[{"name": "../evil", "content": "x"}])
    with pytest.raises(ValueError):
        payload(mcps=[{"name": "a", "url": "https://example.com"}, {"name": "A", "url": "https://example.com"}])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_private_dns_rejected(ip):
    async def run():
        loop = asyncio.get_running_loop()
        original = loop.getaddrinfo
        async def fake(*args, **kwargs):
            return [(None, None, None, None, (ip, 443))]
        loop.getaddrinfo = fake
        try:
            with pytest.raises(m.RunnerError, match="MCP_ADDRESS_FORBIDDEN"):
                await m.validate_remote(m.MCP(name="m", url="https://example.com"))
        finally:
            loop.getaddrinfo = original
    asyncio.run(run())


def test_only_agent_messages(tmp_path):
    events = [
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "TOOL SECRET"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "result": "MCP SECRET"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed"},
    ]
    script = "import sys; sys.stdin.read(); print(" + repr("\n".join(map(json.dumps, events))) + "); print('stderr secret',file=sys.stderr)"
    result = asyncio.run(m.process([sys.executable, "-c", script], tmp_path, {"PATH": "/usr/bin:/bin"}, b"prompt", jsonl=True))
    assert result == "answer"


@pytest.mark.parametrize("script,code", [
    ("print('not json')", "INVALID_CODEX_JSONL"),
    ("print('{}')", "CODEX_NO_FINAL_RESPONSE"),
    ("import sys; print('secret', file=sys.stderr); sys.exit(1)", "CODEX_EXECUTION_FAILED"),
    ("print('{\"type\":\"turn.failed\",\"error\":\"secret\"}')", "CODEX_EXECUTION_FAILED"),
    ("print('{\"type\":\"error\",\"message\":\"stream failed\"}')", "CODEX_NO_FINAL_RESPONSE"),
])
def test_safe_process_errors(tmp_path, script, code):
    with pytest.raises(m.RunnerError, match=code):
        asyncio.run(m.process([sys.executable, "-c", script], tmp_path, {}, jsonl=True))


def test_transient_reconnect_notices_do_not_fail_completed_turn(tmp_path):
    events = [{"type": "thread.started"}, {"type": "turn.started"},
              {"type": "error", "message": "Reconnecting... 2/5 (request timed out)"},
              {"type": "item.completed", "item": {"type": "error", "message": "Falling back from WebSockets to HTTPS transport."}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
              {"type": "turn.completed"}]
    script = "import json\n" + "".join(f"print(json.dumps({e!r}))\n" for e in events)
    result = asyncio.run(m.process([sys.executable, "-c", script], tmp_path, {}, jsonl=True))
    assert result == "answer"


def test_output_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_OUTPUT", 100)
    with pytest.raises(m.RunnerError, match="OUTPUT_LIMIT_EXCEEDED"):
        asyncio.run(m.process([sys.executable, "-c", "print('x'*1000)"], tmp_path, {}))


def test_cancellation_kills_process_group(tmp_path):
    async def run():
        pidfile = tmp_path / "pid"
        script = "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(30)"
        task = asyncio.create_task(m.process([sys.executable, "-c", script], tmp_path, {}))
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    asyncio.run(run())


def test_busy_and_body_limit(monkeypatch):
    with TestClient(m.app) as client:
        headers = {"Authorization": "Bearer " + "t" * 40}
        m.app.state.active = 2
        assert client.post("/execute", headers=headers, json=payload().model_dump()).status_code == 429
        m.app.state.active = 0
        monkeypatch.setattr(m, "MAX_BODY", 8)
        assert client.post("/execute", headers=headers, content=b"x" * 9).status_code == 413
        assert m.app.state.active == 0


def test_execute_args_redaction_cleanup(monkeypatch):
    paths = []
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(m.sys, "platform", "darwin")
    async def fake(argv, cwd, env, stdin=b"", **kwargs):
        paths.append(cwd.parent)
        assert argv[-1] == "-" and stdin == b"hello"
        assert "--ephemeral" in argv and "read-only" in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        return "secret-model-key answer"
    monkeypatch.setattr(m, "process", fake)
    assert asyncio.run(m.execute(payload(), "secret-model-key")) == "[REDACTED] answer"
    assert all(not p.exists() for p in paths)


def simulated_sandbox(monkeypatch, tmp_path, *, write, sockets="allowed", connect=None):
    """Makes the probe see a filesystem and network behaving as the given sandbox would."""
    monkeypatch.chdir(tmp_path)
    denied = PermissionError(1, "Operation not permitted")
    if write == "denied":
        monkeypatch.setattr(probe.pathlib.Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(denied))
    if sockets == "denied":
        monkeypatch.setattr(probe.socket, "socket", lambda *a, **k: (_ for _ in ()).throw(denied))
    else:
        class Socket:
            def settimeout(self, seconds):
                pass

            def connect(self, address):
                if connect is not None:
                    raise connect
        monkeypatch.setattr(probe.socket, "socket", Socket)
    return probe.check()


def test_probe_detects_a_writable_filesystem(monkeypatch, tmp_path):
    assert simulated_sandbox(monkeypatch, tmp_path, write="allowed") == probe.WRITABLE == 41


def test_probe_detects_outbound_network_when_writing_is_blocked(monkeypatch, tmp_path):
    assert simulated_sandbox(monkeypatch, tmp_path, write="denied") == probe.NETWORK == 42


def test_probe_passes_when_the_sandbox_refuses_to_create_a_socket(monkeypatch, tmp_path):
    """Codex's Linux sandbox removes sockets altogether; that is enforcement, not a failure (it used to crash the probe)."""
    assert simulated_sandbox(monkeypatch, tmp_path, write="denied", sockets="denied") == 0


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionRefusedError(111, "refused"), OSError(101, "unreachable")])
def test_probe_passes_when_connections_fail(monkeypatch, tmp_path, error):
    assert simulated_sandbox(monkeypatch, tmp_path, write="denied", connect=error) == 0


def test_the_runner_executes_the_fixed_probe_file_not_a_string():
    assert m.SANDBOX_PROBE == Path(__file__).resolve().with_name("sandbox_probe.py") and m.SANDBOX_PROBE.is_file()


def test_linux_fail_closed(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(m.sys, "platform", "linux")
    calls = []
    async def fail(argv, *args, **kwargs):
        calls.append(argv)
        raise m.RunnerError("CODEX_EXECUTION_FAILED")
    monkeypatch.setattr(m, "process", fail)
    with pytest.raises(m.RunnerError, match="SANDBOX_UNAVAILABLE"):
        asyncio.run(m.execute(payload(), "key"))
    assert len(calls) == 1 and calls[0][1] == "sandbox"


def test_timeout(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setattr(m, "TIMEOUT", 0.01)
    async def stall(*args, **kwargs):
        await asyncio.sleep(30)
    monkeypatch.setattr(m, "process", stall)
    with pytest.raises(m.RunnerError, match="RUN_TIMEOUT"):
        asyncio.run(m.execute(payload(), "key"))


# Local pytest fixtures only; never read from a real credential source.
MOCK_ACCESS_TOKEN = '-'.join(['test', 'access'])
MOCK_REFRESH_TOKEN = '-'.join(['test', 'refresh'])
MOCK_ID_TOKEN = '-'.join(['test', 'id'])
MOCK_ACCOUNT_ID = '-'.join(['test', 'account'])


def oauth_file(tmp_path, monkeypatch):
    source = tmp_path / "auth.json"
    source.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"access_token": MOCK_ACCESS_TOKEN, "refresh_token": MOCK_REFRESH_TOKEN,
                   "id_token": MOCK_ID_TOKEN, "account_id": MOCK_ACCOUNT_ID}}))
    source.chmod(0o600)
    monkeypatch.setenv("CODEX_AUTH_MODE", "chatgpt")
    monkeypatch.setenv("CODEX_OAUTH_AUTH_FILE", str(source))
    return source


def test_oauth_selection_and_isolation(tmp_path, monkeypatch):
    source = oauth_file(tmp_path, monkeypatch)
    assert m.configured() == (None, None)
    root = tmp_path / "run"
    root.mkdir()
    cwd, env = m.prepare(root, payload(), None)
    assert "CODEX_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert "CODEX_OAUTH_AUTH_FILE" not in env
    assert env["CODEX_HOME"] != str(source.parent)
    config = tomllib.loads((root / "codex/config.toml").read_text())
    assert config["forced_login_method"] == "chatgpt"
    assert config["cli_auth_credentials_store"] == "file"
    monkeypatch.setenv("CODEX_AUTH_MODE", "invalid")
    assert m.configured()[1] == "INVALID_CODEX_AUTH_MODE"
    monkeypatch.setenv("CODEX_AUTH_MODE", "chatgpt")
    monkeypatch.setenv("CODEX_OAUTH_AUTH_FILE", "relative/auth.json")
    assert m.configured()[1] == "OAUTH_SOURCE_NOT_CONFIGURED"


@pytest.mark.parametrize("invalid", ["permissions", "symlink", "api", "truncated", "missing-token"])
def test_oauth_invalid_source(tmp_path, monkeypatch, invalid):
    source = oauth_file(tmp_path, monkeypatch)
    if invalid == "permissions":
        source.chmod(0o644)
    elif invalid == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(source)
        monkeypatch.setenv("CODEX_OAUTH_AUTH_FILE", str(link))
    elif invalid == "api":
        source.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"secret"}')
    elif invalid == "truncated":
        source.write_text('{')
    else:
        data = json.loads(source.read_bytes())
        del data["tokens"]["refresh_token"]
        source.write_text(json.dumps(data))
    assert m.configured()[1] == "OAUTH_CREDENTIALS_INVALID"


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_oauth_refresh_persisted_and_redacted(tmp_path, monkeypatch, outcome):
    source = oauth_file(tmp_path, monkeypatch)
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(m.sys, "platform", "darwin")
    roots = []
    async def fake(argv, cwd, env, stdin=b"", **kwargs):
        roots.append(cwd.parent)
        path = Path(env["CODEX_HOME"]) / "auth.json"
        assert path.stat().st_mode & 0o777 == 0o600
        data = json.loads(path.read_bytes())
        data["tokens"]["access_token"] = "new-access"
        path.write_text(json.dumps(data))
        if outcome == "failure":
            raise m.RunnerError("CODEX_EXECUTION_FAILED")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        return MOCK_ACCESS_TOKEN + " new-access answer"
    monkeypatch.setattr(m, "process", fake)
    if outcome == "success":
        assert asyncio.run(m.execute(payload(), None)) == "[REDACTED] [REDACTED] answer"
    else:
        with pytest.raises(m.RunnerError if outcome == "failure" else asyncio.CancelledError):
            asyncio.run(m.execute(payload(), None))
    assert json.loads(source.read_bytes())["tokens"]["access_token"] == "new-access"
    assert source.stat().st_mode & 0o777 == 0o600
    assert all(not root.exists() for root in roots)
    assert not list(tmp_path.glob(".auth-*"))


def test_oauth_lock_and_external_conflict(tmp_path, monkeypatch):
    source = oauth_file(tmp_path, monkeypatch)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir(); two.mkdir()
    with m.oauth_session(one):
        with pytest.raises(m.RunnerError, match="OAUTH_BUSY"):
            with m.oauth_session(two):
                pytest.fail("overlapping OAuth execution admitted")
    with pytest.raises(m.RunnerError, match="OAUTH_SOURCE_CHANGED"):
        with m.oauth_session(two):
            data = json.loads(source.read_bytes())
            data["tokens"]["access_token"] = "external-login"
            source.write_text(json.dumps(data))
    assert json.loads(source.read_bytes())["tokens"]["access_token"] == "external-login"
    three = tmp_path / "three"
    three.mkdir()
    with m.oauth_session(three):
        assert json.loads((three / "auth.json").read_bytes())["tokens"]["access_token"] == "external-login"


def test_oauth_invalid_refresh_does_not_overwrite(tmp_path, monkeypatch):
    source = oauth_file(tmp_path, monkeypatch)
    original = source.read_bytes()
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    with pytest.raises(m.RunnerError, match="OAUTH_CREDENTIALS_INVALID"):
        with m.oauth_session(isolated):
            (isolated / "auth.json").write_text("{")
    assert source.read_bytes() == original
