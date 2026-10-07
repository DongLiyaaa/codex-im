"""Offline tests for the detailed /status integration check. Install pytest==8.3.5 httpx==0.28.1 alongside requirements."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("hub_runner_status", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)

TOKEN = "t" * 40
CANARY = "leak-canary-9f31c7d2"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("RUNNER_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)
    monkeypatch.setenv("CODEX_MODEL", "demo-model")
    monkeypatch.setenv("CODEX_BASE_URL", "https://models.example.com/v1")
    for name in ("CODEX_API_KEY", "CODEX_AUTH_MODE", "CODEX_OAUTH_AUTH_FILE", "CODEX_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    m._status_cache.update(at=-1e9, body=None)
    yield
    m._status_cache.update(at=-1e9, body=None)


def healthy(monkeypatch, *, platform="linux", sandbox=None, probe=None, version="0.157.1", installed=True):
    """A runner whose collaborators answer as given; returns the list of probe calls."""
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/local/bin/codex" if installed else None)
    monkeypatch.setattr(m.sys, "platform", platform)
    calls = []

    async def fake_version(_):
        return version

    async def fake_sandbox():
        return sandbox

    async def fake_probe(endpoint, model, key, transport=None):
        calls.append((endpoint, model))
        return probe or {"state": "ok", "http_status": 200, "model_listed": True}
    monkeypatch.setattr(m, "codex_version", fake_version)
    monkeypatch.setattr(m, "sandbox_error", fake_sandbox)
    monkeypatch.setattr(m, "probe_model_endpoint", fake_probe)
    return calls


def get(path="/status", headers=AUTH):
    with TestClient(m.app) as client:
        return client.get(path, headers=headers)


def test_status_needs_the_runner_token(monkeypatch):
    healthy(monkeypatch)
    assert get(headers={}).status_code == 401
    assert get(headers={"Authorization": "Bearer " + "x" * 40}).status_code == 401
    assert get(headers={"Authorization": TOKEN}).status_code == 401  # Not a Bearer header.
    monkeypatch.setenv("RUNNER_TOKEN", "short")
    assert get().json() == {"detail": "RUNNER_TOKEN_NOT_CONFIGURED"}


def test_ready_runner_reports_what_the_home_page_needs(monkeypatch):
    calls = healthy(monkeypatch)
    response = get()
    body = response.json()
    assert response.status_code == 200 and body["ready"] is True
    assert body["auth_mode"] == "api" and body["config_error"] is None
    assert body["codex"] == {"installed": True, "version": "0.157.1", "pinned_version": m.VERSION}
    assert body["model"] == {"id": "demo-model", "endpoint_host": "models.example.com", "credential_configured": True}
    assert body["sandbox"] == {"state": "ok", "error": None}
    assert body["model_endpoint"]["state"] == "ok"
    assert calls == [("https://models.example.com/v1", "demo-model")]


def test_status_never_returns_the_key_or_a_full_url(monkeypatch):
    healthy(monkeypatch)
    text = get().text
    assert CANARY not in text
    assert "https://" not in text and "/v1" not in text  # Host only: the path and scheme stay on the runner.


def test_failed_sandbox_is_reported_alongside_everything_else(monkeypatch):
    healthy(monkeypatch, sandbox="SANDBOX_UNAVAILABLE")
    body = get().json()
    assert body["ready"] is False
    assert body["sandbox"] == {"state": "failed", "error": "SANDBOX_UNAVAILABLE"}
    assert body["model_endpoint"]["state"] == "ok"  # The model side is still checked, so both problems show at once.


def test_missing_credentials_skip_the_probe_but_keep_the_other_facts(monkeypatch):
    calls = healthy(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY")
    body = get().json()
    assert body["ready"] is False and body["config_error"] == "MODEL_API_KEY_NOT_CONFIGURED"
    assert body["model"]["credential_configured"] is False
    assert body["model_endpoint"] == {"state": "skipped", "reason": "CONFIG_ERROR"}
    assert calls == []  # Without a key there is nothing to send.
    assert body["codex"]["installed"] is True


def test_invalid_endpoint_is_a_config_error_not_a_crash(monkeypatch):
    calls = healthy(monkeypatch)
    monkeypatch.setenv("CODEX_BASE_URL", "http://models.example.com/v1")
    body = get().json()
    assert body["ready"] is False and body["config_error"] == "INVALID_CODEX_BASE_URL"
    assert body["model"]["id"] is None and body["model"]["endpoint_host"] is None
    assert calls == []


def test_codex_cli_missing(monkeypatch):
    healthy(monkeypatch, installed=False, version=None)
    body = get().json()
    assert body["ready"] is False
    assert body["codex"]["installed"] is False and body["codex"]["version"] is None
    assert body["sandbox"]["state"] == "skipped"


def test_sandbox_is_not_probed_outside_linux(monkeypatch):
    healthy(monkeypatch, platform="darwin", sandbox="SANDBOX_UNAVAILABLE")  # Would fail if it were consulted.
    body = get().json()
    assert body["sandbox"] == {"state": "skipped", "error": None} and body["ready"] is True


def test_default_endpoint_is_not_probed(monkeypatch):
    calls = healthy(monkeypatch)
    monkeypatch.delenv("CODEX_BASE_URL")
    body = get().json()
    assert body["model_endpoint"] == {"state": "skipped", "reason": "NO_CUSTOM_ENDPOINT"}
    assert body["model"]["endpoint_host"] is None and body["ready"] is True and calls == []


@pytest.mark.parametrize("state", sorted(m.PROBE_FAILURES))
def test_a_failing_model_endpoint_makes_the_runner_not_ready(monkeypatch, state):
    healthy(monkeypatch, probe={"state": state, "http_status": None, "model_listed": None})
    assert get().json()["ready"] is False


@pytest.mark.parametrize("state", ["unverified", "skipped"])
def test_an_unverifiable_model_endpoint_does_not_block(monkeypatch, state):
    healthy(monkeypatch, probe={"state": state})
    assert get().json()["ready"] is True


def test_chatgpt_login_mode_reports_credentials_from_the_login(monkeypatch):
    healthy(monkeypatch)
    monkeypatch.delenv("CODEX_BASE_URL")
    monkeypatch.setenv("CODEX_AUTH_MODE", "chatgpt")
    monkeypatch.delenv("OPENAI_API_KEY")
    body = get().json()
    assert body["auth_mode"] == "chatgpt" and body["config_error"] == "OAUTH_SOURCE_NOT_CONFIGURED"
    assert body["model"]["credential_configured"] is False and body["ready"] is False


def test_results_are_cached_and_a_refresh_cannot_hammer_the_endpoint(monkeypatch):
    calls = healthy(monkeypatch)
    with TestClient(m.app) as client:
        first = client.get("/status", headers=AUTH).json()
        assert client.get("/status?refresh=true", headers=AUTH).json() == first  # Inside the minimum interval.
        assert len(calls) == 1
        m._status_cache["at"] -= m.STATUS_MIN_INTERVAL + 1  # Old enough for an explicit refresh, not for the TTL.
        client.get("/status", headers=AUTH)
        assert len(calls) == 1
        client.get("/status?refresh=true", headers=AUTH)
        assert len(calls) == 2
        m._status_cache["at"] -= m.STATUS_TTL + 1
        client.get("/status", headers=AUTH)
        assert len(calls) == 3


def test_a_stuck_check_gives_a_static_timeout_error(monkeypatch):
    healthy(monkeypatch)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(60)
    monkeypatch.setattr(m, "probe_model_endpoint", hang)
    monkeypatch.setattr(m, "STATUS_DEADLINE", 0.2)
    response = get()
    assert response.status_code == 504 and response.json() == {"detail": "STATUS_TIMEOUT"}
    assert m._status_cache["body"] is None  # A failed check is not remembered as a result.


def test_health_is_unchanged_by_the_status_check(monkeypatch):
    healthy(monkeypatch, probe={"state": "unauthorized"})
    with TestClient(m.app) as client:
        assert set(client.get("/health").json()) == {"status", "error", "codex_version"}


# ---- The endpoint probe itself, against a fake provider -------------------------------------------------------

def run_probe(handler, model="demo-model", endpoint="https://models.example.com/v1"):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    result = asyncio.run(m.probe_model_endpoint(endpoint, model, CANARY, transport=httpx.MockTransport(wrapped)))
    return result, seen


def listing(*ids):
    return httpx.Response(200, json={"object": "list", "data": [{"id": name} for name in ids]})


def test_probe_asks_the_configured_endpoint_for_its_models():
    result, seen = run_probe(lambda _: listing("other", "demo-model"))
    assert result == {"state": "ok", "http_status": 200, "model_listed": True}
    assert len(seen) == 1 and str(seen[0].url) == "https://models.example.com/v1/models" and seen[0].method == "GET"
    assert seen[0].headers["authorization"] == f"Bearer {CANARY}"


def test_probe_reports_a_missing_model():
    result, _ = run_probe(lambda _: listing("other"))
    assert result == {"state": "model_missing", "http_status": 200, "model_listed": False}


def test_probe_without_a_configured_model_only_checks_reachability():
    result, _ = run_probe(lambda _: listing("other"), model=None)
    assert result == {"state": "ok", "http_status": 200, "model_listed": None}


def test_probe_never_follows_a_redirect_with_the_key():
    def handler(request):
        if request.url.host == "models.example.com":
            return httpx.Response(302, headers={"location": "https://elsewhere.example.net/steal"})
        raise AssertionError("the key was sent to a redirect target")
    result, seen = run_probe(handler)
    assert result["state"] == "redirect_refused" and result["http_status"] == 302
    assert [request.url.host for request in seen] == ["models.example.com"]


@pytest.mark.parametrize("code,state", [(401, "unauthorized"), (403, "unauthorized"), (404, "unverified"),
                                        (405, "unverified"), (501, "unverified"), (429, "http_error"),
                                        (500, "http_error"), (503, "http_error")])
def test_probe_status_codes(code, state):
    result, _ = run_probe(lambda _: httpx.Response(code, text=f"provider says {CANARY}"))
    assert result["state"] == state and result["http_status"] == code
    assert CANARY not in json.dumps(result)  # A provider body is never passed on.


@pytest.mark.parametrize("body", [b"not json", b'{"data": "no"}', b"[]", b"{}", b'{"data": null}'])
def test_probe_rejects_malformed_listings(body):
    result, _ = run_probe(lambda _: httpx.Response(200, content=body))
    assert result["state"] == "invalid_response"


def test_probe_bounds_the_response_it_reads():
    result, _ = run_probe(lambda _: httpx.Response(200, content=b"x" * (m.MODELS_LIMIT + 10)))
    assert result["state"] == "invalid_response"


def test_probe_transport_failures():
    def timeout(request):
        raise httpx.ConnectTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("down", request=request)
    assert run_probe(timeout)[0]["state"] == "timeout"
    assert run_probe(refused)[0]["state"] == "unreachable"
    assert "slow" not in json.dumps(run_probe(timeout)[0])


def test_probe_has_an_overall_deadline(monkeypatch):
    async def slow(_request):
        await asyncio.sleep(60)

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await slow(request)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(m.asyncio, "timeout", lambda _seconds: real_timeout(0.2))
    result = asyncio.run(m.probe_model_endpoint("https://models.example.com/v1", None, CANARY, transport=Slow()))
    assert result["state"] == "timeout"


def test_socks_proxy_is_not_probed(monkeypatch):
    monkeypatch.setenv("CODEX_PROXY_URL", "socks5://127.0.0.1:7890")
    result, seen = run_probe(lambda _: listing("demo-model"))
    assert result == {"state": "skipped", "reason": "SOCKS_PROXY_NOT_PROBED"} and seen == []
