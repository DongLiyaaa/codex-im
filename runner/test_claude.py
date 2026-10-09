"""Claude CLI agent tests. Offline by default; the real-CLI class runs when CLAUDE_CLI_BIN points at a claude binary."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import zlib

import pytest
from fastapi.testclient import TestClient

import mock_services

spec = importlib.util.spec_from_file_location("hub_runner_claude", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

CAP = "cap." + "a" * 64


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("RUNNER_TOKEN", "t" * 40)
    monkeypatch.setenv("HUB_AGENTS", "codex,claude")
    monkeypatch.setenv("CLAUDE_API_KEY", "sk-ant-secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-model-key")
    for name in ("CLAUDE_MODEL", "CLAUDE_BASE_URL", "CLAUDE_PROXY_URL", "ANTHROPIC_API_KEY", "CODEX_API_KEY",
                 "CODEX_AUTH_MODE", "CODEX_BASE_URL", "CODEX_MODEL", "CODEX_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ATTACHMENT_BRIDGE_URL", "http://backend:8000/internal/attachment-mcp")
    monkeypatch.setenv("PLATFORM_BRIDGE_URL", "http://backend:8000/internal/platform-mcp")
    m.app.state.active = 0

    async def connected(self):
        return False

    # TestClient's receive() blocks until the response completes, which would hang the watcher.
    monkeypatch.setattr("starlette.requests.Request.is_disconnected", connected)


def payload(**kwargs):
    return m.Execute(run_id="r", conversation_id="c", agent="claude", prompt="hello", **kwargs)


# ---- configuration and flags ------------------------------------------------------------------------------

def test_claude_has_no_builtin_tools_and_no_key_in_argv_or_files(tmp_path):
    cwd, env, args = m.prepare_claude(tmp_path, payload(attachment_capability=CAP, platform_capability=CAP), "sk-ant-secret-key")
    assert args[args.index("--tools") + 1] == ""
    for flag in ("--bare", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--permission-prompts") + 1] == "none"
    assert not any("bypass" in a.lower() or a == "--dangerously-skip-permissions" for a in args)
    assert not any("sk-ant" in a or CAP in a for a in args)  # Secrets only travel via env / the private 0600 file.
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-secret-key"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude") and env["HOME"] == str(tmp_path / "home")
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env and "RUNNER_TOKEN" not in env
    mcp = Path(args[args.index("--mcp-config") + 1])
    assert mcp.stat().st_mode & 0o777 == 0o600
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert set(servers) == {"hub_attachments", "hub_personal_platforms"}
    assert servers["hub_attachments"]["headers"] == {"Authorization": "Bearer " + CAP}
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert set(allowed) == ({f"mcp__hub_attachments__{t}" for t in m.ATTACHMENT_TOOLS}
                            | {f"mcp__hub_personal_platforms__{t}" for t in m.PLATFORM_TOOLS})
    assert (tmp_path / "instructions.md").stat().st_mode & 0o777 == 0o600


def test_no_mcp_means_no_mcp_flags(tmp_path):
    _, _, args = m.prepare_claude(tmp_path, payload(), "k")
    assert "--mcp-config" not in args and "--allowedTools" not in args and "--model" not in args


def test_skills_and_user_mcps_reach_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    p = payload(skills=[m.Skill(name="s", content="SKILL BODY")], mcps=[m.MCP(name="docs", url="https://example.com/mcp", headers={"X-Key": "v"})])
    _, _, args = m.prepare_claude(tmp_path, p, "k")
    assert "SKILL BODY" in (tmp_path / "instructions.md").read_text()
    assert args[args.index("--model") + 1] == "claude-sonnet-5"
    assert args[args.index("--allowedTools") + 1] == "mcp__docs"
    assert json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())["mcpServers"]["docs"] == {
        "type": "http", "url": "https://example.com/mcp", "headers": {"X-Key": "v"}}


def test_hub_bridge_names_are_reserved_and_bridges_must_be_configured(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(m.RunnerError, match="RESERVED_MCP_NAME"):
        m.prepare_claude(tmp_path / "a", payload(attachment_capability=CAP,
                                                  mcps=[m.MCP(name="Hub_Attachments", url="https://example.com/mcp")]), "k")
    monkeypatch.setenv("PLATFORM_BRIDGE_URL", "http://backend:8000/elsewhere")
    with pytest.raises(m.RunnerError, match="PLATFORM_BRIDGE_NOT_CONFIGURED"):
        m.prepare_claude(tmp_path / "b", payload(platform_capability=CAP), "k")


def test_custom_endpoint_and_proxy_are_per_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BASE_URL", "https://gateway.example.com/anthropic/")
    monkeypatch.setenv("CLAUDE_PROXY_URL", "http://127.0.0.1:7890")
    monkeypatch.setenv("CODEX_PROXY_URL", "http://127.0.0.1:9999")
    _, env, _ = m.prepare_claude(tmp_path, payload(), "k")
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com/anthropic"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"


@pytest.mark.parametrize("url", ["http://gateway.example.com", "https://localhost/x", "https://10.0.0.1", "https://u:p@g.example.com",
                                 "https://g.example.com/?x=1"])
def test_unsafe_claude_endpoints_are_refused(monkeypatch, url):
    monkeypatch.setenv("CLAUDE_BASE_URL", url)
    assert m.claude_configured() == (None, "INVALID_CLAUDE_BASE_URL")


def test_claude_requires_its_own_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY")
    assert m.claude_configured() == (None, "CLAUDE_API_KEY_NOT_CONFIGURED")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other")
    assert m.claude_configured() == ("other", None)
    monkeypatch.setenv("CLAUDE_MODEL", "bad model!")
    assert m.claude_configured() == (None, "INVALID_CLAUDE_MODEL")


def test_the_codex_key_is_not_a_claude_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY")
    assert m.configured() == ("secret-model-key", None)
    assert m.claude_configured()[1] == "CLAUDE_API_KEY_NOT_CONFIGURED"


def test_enabled_agents(monkeypatch):
    monkeypatch.delenv("HUB_AGENTS")
    assert m.enabled_agents() == ("codex",)
    monkeypatch.setenv("HUB_AGENTS", " Claude , x ")
    assert m.enabled_agents() == ("claude",)
    monkeypatch.setenv("HUB_AGENTS", "")
    assert m.enabled_agents() == ("codex",)


# ---- stream parsing ---------------------------------------------------------------------------------------

def init(tools=(), servers=(("a", "connected"),)):
    return {"type": "system", "subtype": "init", "tools": list(tools),
            "mcp_servers": [{"name": n, "status": s} for n, s in servers]}


def result(text="answer", **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}


def run_stream(tmp_path, lines, servers=("a",), code=0, stderr=""):
    body = "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines)
    script = ("import sys; sys.stdin.read(); print(" + repr(body) + "); print(" + repr(stderr)
              + ", file=sys.stderr); sys.exit(" + str(code) + ")")
    return asyncio.run(m.process([sys.executable, "-c", script], tmp_path, {"PATH": "/usr/bin:/bin"}, b"p",
                                 stream=m.ClaudeStream(set(servers))))


def test_final_result_is_returned_and_noise_is_ignored(tmp_path):
    lines = ["[claude-code:notice] {\"x\":1}", init(["mcp__a__t"], [("a", "connected")]),
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking aloud"}]}},
             result("final")]
    assert run_stream(tmp_path, lines, ["a"]) == "final"


@pytest.mark.parametrize("lines,code", [
    ([init(), {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "secret"}], "CLAUDE_EXECUTION_FAILED"),
    ([init(), result(is_error=True)], "CLAUDE_EXECUTION_FAILED"),
    ([init()], "CLAUDE_NO_FINAL_RESPONSE"),
    ([result()], "CLAUDE_NO_FINAL_RESPONSE"),
    ([init(), result("  ")], "CLAUDE_NO_FINAL_RESPONSE"),
    ([init(["Bash"]), result()], "CLAUDE_UNEXPECTED_TOOLS"),
    ([init(["mcp__other__t"], [("a", "connected")]), result()], "CLAUDE_UNEXPECTED_TOOLS"),
    ([init([], [("a", "failed")]), result()], "CLAUDE_MCP_UNAVAILABLE"),
    ([init([], [("a", "connected"), ("b", "connected")]), result()], "CLAUDE_UNEXPECTED_MCP"),
    ([init([], []), result()], "CLAUDE_UNEXPECTED_MCP"),
    ([{"type": "system", "subtype": "init"}, result()], "CLAUDE_INIT_INVALID"),
])
def test_stream_failures_are_static_codes(tmp_path, lines, code):
    with pytest.raises(m.RunnerError, match=code) as exc:
        run_stream(tmp_path, lines, ["a"])
    assert "secret" not in str(exc.value.code)


def test_nonzero_exit_and_stderr_never_leak(tmp_path):
    with pytest.raises(m.RunnerError) as exc:
        run_stream(tmp_path, [init(), result()], code=1, stderr="TOP SECRET")
    assert exc.value.code == "CLAUDE_EXECUTION_FAILED"


def test_response_limit(tmp_path):
    with pytest.raises(m.RunnerError, match="RESPONSE_LIMIT_EXCEEDED"):
        run_stream(tmp_path, [init(), result("x" * (m.MAX_TEXT + 1))])


def test_output_limit_applies_to_claude_too(tmp_path):
    script = "print('x' * 2_000_000)"
    with pytest.raises(m.RunnerError, match="OUTPUT_LIMIT_EXCEEDED"):
        asyncio.run(m.process([sys.executable, "-c", script], tmp_path, {}, stream=m.ClaudeStream(set())))


# ---- execute dispatch -------------------------------------------------------------------------------------

def test_execute_claude_args_redaction_and_cleanup(monkeypatch):
    dirs = []
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/local/bin/" + name)

    async def fake(argv, cwd, env, stdin=b"", **kwargs):
        dirs.append(cwd.parent)
        assert argv[0] == "/usr/local/bin/claude" and stdin == b"hello"
        assert isinstance(kwargs["stream"], m.ClaudeStream)
        return "sk-ant-secret-key and " + CAP + " answer"
    monkeypatch.setattr(m, "process", fake)
    text = asyncio.run(m.execute(payload(attachment_capability=CAP), "sk-ant-secret-key"))
    assert text == "[REDACTED] and [REDACTED] answer" and all(not d.exists() for d in dirs)


def test_codex_requests_still_use_the_codex_path(monkeypatch):
    seen = []

    async def fake_codex(p, key):
        seen.append(("codex", key))
        return "c"

    async def fake_claude(p, key):
        seen.append(("claude", key))
        return "k"
    monkeypatch.setattr(m, "execute_codex", fake_codex)
    monkeypatch.setattr(m, "execute_claude", fake_claude)
    assert asyncio.run(m.execute(m.Execute(run_id="r", conversation_id="c", prompt="x"), "ck")) == "c"
    assert asyncio.run(m.execute(payload(), "kk")) == "k"
    assert seen == [("codex", "ck"), ("claude", "kk")]


def test_missing_cli_is_reported(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    with pytest.raises(m.RunnerError, match="CLAUDE_CLI_NOT_INSTALLED"):
        asyncio.run(m.execute(payload(), "k"))


def post(client, **kwargs):
    return client.post("/execute", headers={"Authorization": "Bearer " + "t" * 40}, json=payload(**kwargs).model_dump())


def test_endpoint_routes_by_agent(monkeypatch):
    calls = []

    async def fake(p, key):
        calls.append((p.agent, key))
        return "ok"
    monkeypatch.setattr(m, "execute", fake)
    with TestClient(m.app) as client:
        assert post(client).json() == {"text": "ok"}
        legacy = client.post("/execute", headers={"Authorization": "Bearer " + "t" * 40},
                             json={"run_id": "r", "conversation_id": "c", "prompt": "x"})
        assert legacy.json() == {"text": "ok"}  # Requests without an agent are Codex requests.
    assert calls == [("claude", "sk-ant-secret-key"), ("codex", "secret-model-key")]


def test_endpoint_refuses_disabled_or_unconfigured_agents(monkeypatch):
    with TestClient(m.app) as client:
        monkeypatch.setenv("HUB_AGENTS", "codex")
        assert post(client).status_code == 422 and post(client).json() == {"detail": "AGENT_NOT_ENABLED"}
        monkeypatch.setenv("HUB_AGENTS", "codex,claude")
        monkeypatch.delenv("CLAUDE_API_KEY")
        response = post(client)
        assert response.status_code == 503 and response.json() == {"detail": "CLAUDE_API_KEY_NOT_CONFIGURED"}


def test_claude_misconfiguration_does_not_block_codex(monkeypatch):
    async def fake(p, key):
        return "ok"
    monkeypatch.setattr(m, "execute", fake)
    monkeypatch.delenv("CLAUDE_API_KEY")
    with TestClient(m.app) as client:
        legacy = client.post("/execute", headers={"Authorization": "Bearer " + "t" * 40},
                             json={"run_id": "r", "conversation_id": "c", "prompt": "x"})
    assert legacy.status_code == 200


def test_health_lists_each_enabled_agent(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(m.sys, "platform", "darwin")
    with TestClient(m.app) as client:
        body = client.get("/health").json()
        assert body["status"] == "ready" and body["claude_version"] == m.CLAUDE_VERSION
        assert body["agents"]["claude"] == {"enabled": True, "ready": True, "error": None}
        monkeypatch.delenv("CLAUDE_API_KEY")
        body = client.get("/health").json()
        assert body["status"] == "ready"  # Codex still works.
        assert body["agents"]["claude"]["error"] == "CLAUDE_API_KEY_NOT_CONFIGURED"
        assert body["agents"]["codex"]["ready"] is True


def test_health_when_only_claude_is_enabled_and_broken(monkeypatch):
    monkeypatch.setenv("HUB_AGENTS", "claude")
    monkeypatch.delenv("CLAUDE_API_KEY")
    with TestClient(m.app) as client:
        body = client.get("/health").json()
    assert body["status"] == "not_ready" and body["error"] == "CLAUDE_API_KEY_NOT_CONFIGURED"
    assert body["agents"]["codex"]["enabled"] is False


def test_status_reports_claude_without_secrets(monkeypatch):
    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(m.sys, "platform", "darwin")
    monkeypatch.setenv("CLAUDE_BASE_URL", "https://gateway.example.com/anthropic")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5")
    seen = {}

    async def fake_probe(endpoint, model, key, transport=None, agent="codex"):
        seen.update(endpoint=endpoint, model=model, agent=agent)
        return {"state": "ok", "http_status": 200, "model_listed": True}

    async def fake_version(executable):
        return "2.1.286"
    monkeypatch.setattr(m, "probe_model_endpoint", fake_probe)
    monkeypatch.setattr(m, "codex_version", fake_version)
    body = asyncio.run(m.build_claude_status())
    assert seen == {"endpoint": "https://gateway.example.com/anthropic", "model": "claude-sonnet-5", "agent": "claude"}
    assert body["ready"] is True and body["version"] == "2.1.286" and body["pinned_version"] == m.CLAUDE_VERSION
    assert body["model"] == {"id": "claude-sonnet-5", "endpoint_host": "gateway.example.com", "credential_configured": True}
    assert "sk-ant" not in json.dumps(body) and "/anthropic" not in json.dumps(body)


def test_claude_probe_uses_the_anthropic_headers_and_path(monkeypatch):
    import httpx
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), key=request.headers.get("x-api-key"), version=request.headers.get("anthropic-version"),
                    authorization=request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}]})
    body = asyncio.run(m.probe_model_endpoint("https://gateway.example.com/anthropic", "claude-sonnet-5", "k",
                                              transport=httpx.MockTransport(handler), agent="claude"))
    assert body["state"] == "ok" and seen == {"url": "https://gateway.example.com/anthropic/v1/models", "key": "k",
                                              "version": "2023-06-01", "authorization": None}


# ---- the real Claude CLI against local mock services ------------------------------------------------------

def png() -> bytes:
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


CLI = os.environ.get("CLAUDE_CLI_BIN")


@pytest.mark.skipif(not CLI, reason="set CLAUDE_CLI_BIN to a claude binary (npm i @anthropic-ai/claude-code@2.1.286)")
class TestRealCli:
    @pytest.fixture
    def services(self, monkeypatch):
        recorder = mock_services.Recorder()
        api = mock_services.serve(mock_services.anthropic(recorder))
        mcp = mock_services.serve(mock_services.mcp_server(recorder))
        monkeypatch.setattr(m.shutil, "which", lambda name: CLI)
        # The production check demands a public https endpoint; the local mock is plain http on loopback.
        monkeypatch.setattr(m, "model_settings", lambda agent="codex": ("claude-mock", f"http://127.0.0.1:{api.server_port}"))
        monkeypatch.setenv("ATTACHMENT_BRIDGE_URL", f"http://127.0.0.1:{mcp.server_port}/internal/attachment-mcp")
        monkeypatch.setenv("PATH", os.environ["PATH"])
        yield recorder
        api.shutdown()
        mcp.shutdown()

    def test_plain_answer_has_no_builtin_tools(self, services):
        text = asyncio.run(m.execute(payload(), "sk-ant-test"))
        assert text == "MOCK-ANSWER tools=none"
        assert services.api and all(call["key"] == "sk-ant-test" for call in services.api)

    def test_granted_tool_runs_with_the_capability_header(self, services):
        text = asyncio.run(m.execute(m.Execute(run_id="r", conversation_id="c", agent="claude", prompt="USE_TOOL:read_document",
                                               attachment_capability=CAP), "sk-ant-test"))
        assert text.endswith("TOOLRESULT")
        calls = [c for c in services.mcp if c["method"] == "tools/call"]
        assert len(calls) == 1 and calls[0]["authorization"] == "Bearer " + CAP

    def test_a_tool_outside_the_grant_is_denied(self, services):
        text = asyncio.run(m.execute(m.Execute(run_id="r", conversation_id="c", agent="claude", prompt="USE_TOOL:not_granted",
                                               attachment_capability=CAP), "sk-ant-test"))
        assert not [c for c in services.mcp if c["method"] == "tools/call"]
        assert text.startswith("MOCK-ANSWER")

    def test_skill_is_in_the_system_prompt(self, services):
        asyncio.run(m.execute(m.Execute(run_id="r", conversation_id="c", agent="claude", prompt="hi",
                                        skills=[m.Skill(name="s", content="SKILLDOC-MARK")]), "sk-ant-test"))
        assert "SKILLDOC-MARK" in json.dumps(services.api[0]["request"]["system"])

    def test_images_reach_the_model_as_image_blocks(self, services, monkeypatch):
        data = png()

        async def fake_fetch(p, cwd):
            target = cwd / "a.png"
            target.write_bytes(data)
            return [target]
        monkeypatch.setattr(m, "fetch_image_files", fake_fetch)
        asyncio.run(m.execute(payload(), "sk-ant-test"))
        blocks = [b for call in services.api for msg in call["request"]["messages"]
                  if isinstance(msg["content"], list) for b in msg["content"] if b.get("type") == "image"]
        assert blocks and blocks[0]["source"]["data"] == base64.b64encode(data).decode()

    def test_unreachable_required_mcp_fails_closed(self, services, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_BRIDGE_URL", "http://127.0.0.1:9/internal/attachment-mcp")
        with pytest.raises(m.RunnerError) as exc:
            asyncio.run(m.execute(m.Execute(run_id="r", conversation_id="c", agent="claude", prompt="hi",
                                            attachment_capability=CAP), "sk-ant-test"))
        assert exc.value.code == "CLAUDE_MCP_UNAVAILABLE"
        # The CLI may already have called the model, but never with tools, and the run's output is discarded.
        assert all(not call["request"].get("tools") for call in services.api)
