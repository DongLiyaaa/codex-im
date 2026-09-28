"""Isolated Codex 0.157.1 runner; run with uvicorn main:app --host 0.0.0.0 --port 8081.

HTTP MCP runs in the Codex parent, outside command sandbox network restrictions.
Deployment must enforce network egress policy against DNS rebinding/redirect SSRF.
No command tool or unsafe sandbox fallback is exposed to the model.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import stat
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sys
import tempfile
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

VERSION = "0.157.1"
MAX_BODY = 512_000
MAX_OUTPUT = 1_048_576
MAX_TEXT = 128_000
TIMEOUT = 180
NAME = r"^[a-zA-Z0-9_-]{1,64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Skill(StrictModel):
    name: str = Field(pattern=NAME)
    content: str = Field(min_length=1, max_length=64_000)


class MCP(StrictModel):
    name: str = Field(pattern=NAME)
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("url")
    @classmethod
    def https_only(cls, value: str) -> str:
        try:
            u = urlsplit(value)
            if (u.scheme != "https" or not u.hostname or u.username or u.password
                    or u.fragment or u.port not in (None, 443) or any(c.isspace() for c in value)):
                raise ValueError()
        except ValueError:
            raise ValueError("MCP requires an HTTPS URL on port 443 without credentials or fragment") from None
        return value

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        for k, v in headers.items():
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", k)
                    or k.lower() in {"host", "connection", "content-length", "transfer-encoding"}
                    or len(v) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in v)):
                raise ValueError("Invalid MCP header")
        return headers


class Execute(StrictModel):
    run_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=64_000)
    skills: list[Skill] = Field(default_factory=list, max_length=32)
    mcps: list[MCP] = Field(default_factory=list, max_length=16)

    @field_validator("skills", "mcps")
    @classmethod
    def unique_names(cls, values):
        if len({v.name.lower() for v in values}) != len(values):
            raise ValueError("Duplicate resource names")
        return values


class RunnerError(Exception):
    def __init__(self, code: str, status: int = 502):
        self.code, self.status = code, status


def oauth_source() -> Path | None:
    value = os.environ.get("CODEX_OAUTH_AUTH_FILE", "")
    if not value or not Path(value).is_absolute():
        return None
    return Path(value)


def read_oauth(path: Path) -> bytes:
    """Read only the explicitly selected, private regular file; never the host home."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError()
            raw = stream.read(65537)
        data = json.loads(raw)
        tokens = data.get("tokens")
        if (len(raw) > 65536 or data.get("auth_mode") != "chatgpt"
                or data.get("OPENAI_API_KEY") or not isinstance(tokens, dict)
                or not all(isinstance(tokens.get(k), str) and tokens[k]
                           for k in ("access_token", "refresh_token", "id_token"))):
            raise ValueError()
        return raw
    except (OSError, ValueError, TypeError, AttributeError):
        raise RunnerError("OAUTH_CREDENTIALS_INVALID", 503) from None


@contextlib.contextmanager
def oauth_session(codex: Path):
    source = oauth_source()
    if source is None:
        raise RunnerError("OAUTH_SOURCE_NOT_CONFIGURED", 503)
    # Lock a stable sibling inode, not auth.json which is atomically replaced.
    fd = os.open(source.with_name(source.name + ".lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "rb") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RunnerError("OAUTH_LOCK_INVALID", 503)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunnerError("OAUTH_BUSY", 429) from None
        original = read_oauth(source)
        target = codex / "auth.json"
        target.touch(mode=0o600, exist_ok=False)
        target.write_bytes(original)
        try:
            yield original
        finally:
            # process() has terminated the child before this runs, including on cancellation.
            updated = read_oauth(target)
            if read_oauth(source) != original:
                raise RunnerError("OAUTH_SOURCE_CHANGED", 503)
            if updated != original:
                name = None
                try:
                    out, name = tempfile.mkstemp(prefix=".auth-", dir=source.parent)
                    with os.fdopen(out, "wb") as stream:
                        stream.write(updated)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, source)
                finally:
                    if name:
                        Path(name).unlink(missing_ok=True)


def configured() -> tuple[str | None, str | None]:
    token = os.environ.get("RUNNER_TOKEN", "")
    key = os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")
    mode = os.environ.get("CODEX_AUTH_MODE", "api")
    if len(token.encode()) < 32:
        return None, "RUNNER_TOKEN_NOT_CONFIGURED"
    if mode == "chatgpt":
        source = oauth_source()
        if source is None:
            return None, "OAUTH_SOURCE_NOT_CONFIGURED"
        try:
            read_oauth(source)
        except RunnerError as exc:
            return None, exc.code
        return None, None
    if mode != "api":
        return None, "INVALID_CODEX_AUTH_MODE"
    if not key or not key.strip():
        return None, "MODEL_API_KEY_NOT_CONFIGURED"
    return key, None


async def validate_remote(mcp: MCP) -> None:
    host = urlsplit(mcp.url).hostname
    if host is None or host.lower().rstrip(".") in {"localhost", "metadata.google.internal"}:
        raise RunnerError("MCP_ADDRESS_FORBIDDEN", 422)
    try:
        answers = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        raise RunnerError("MCP_DNS_FAILED", 422) from None
    if not answers:
        raise RunnerError("MCP_DNS_FAILED", 422)
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
        if not address.is_global or getattr(address, "ipv4_mapped", None) is not None:
            raise RunnerError("MCP_ADDRESS_FORBIDDEN", 422)


def toml_string(value: str) -> str:
    # JSON string escaping is a subset of TOML basic strings for validated input.
    return json.dumps(value, ensure_ascii=False)


def prepare(root: Path, payload: Execute, key: str | None) -> tuple[Path, dict[str, str]]:
    home, codex, cwd = root / "home", root / "codex", root / "workspace"
    for directory in (home, codex, cwd, root / "tmp"):
        directory.mkdir(mode=0o700)
    env = {
        "HOME": str(home), "CODEX_HOME": str(codex), "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "NO_COLOR": "1",
    }
    if key is not None:
        env["CODEX_API_KEY"] = key
    for skill in payload.skills:
        target = cwd / ".agents" / "skills" / skill.name
        target.mkdir(parents=True, mode=0o700)
        (target / "SKILL.md").write_text(skill.content, encoding="utf-8")
    # Shell is disabled; do not rely on a model file-read tool to load skills.
    # These are only the current request's granted documents. Prompt wording is
    # not an authorization/security boundary; admission and tool config are.
    instructions = (
        "Use only the supplied authorized skill documents and configured MCP tools. "
        "The following skill content is task guidance, not a security boundary or "
        "permission to load other files, install dependencies, or discover other skills. "
        "No local shell or supporting-file access is available. If a skill requires "
        "unprovided supporting files or scripts, report that limitation.\n"
        + json.dumps([s.model_dump() for s in payload.skills], ensure_ascii=True)
    )
    config = [
        f'forced_login_method = {toml_string("api" if key is not None else "chatgpt")}',
        f'developer_instructions = {toml_string(instructions)}',
        'approval_policy = "never"', 'sandbox_mode = "read-only"',
        'cli_auth_credentials_store = "file"', 'mcp_oauth_credentials_store = "file"',
        'web_search = "disabled"', 'allow_login_shell = false',
        '[shell_environment_policy]', 'inherit = "none"',
        '[skills]', 'include_instructions = false',
        '[skills.bundled]', 'enabled = false',
        '[features]', 'shell_tool = false', 'unified_exec = false',
        'apply_patch_freeform = false', 'js_repl = false', 'code_mode = false',
        'multi_agent = false', 'apps = false', 'plugins = false', 'hooks = false',
        'memories = false', 'shell_snapshot = false', 'skill_mcp_dependency_install = false',
        'skill_env_var_dependency_prompt = false', 'skip_host_skill_discovery = true',
    ]
    for mcp in payload.mcps:
        config.extend([
            f'[mcp_servers.{toml_string(mcp.name)}]',
            f'url = {toml_string(mcp.url)}', 'required = true',
            'startup_timeout_sec = 20', 'tool_timeout_sec = 60',
            f'[mcp_servers.{toml_string(mcp.name)}.http_headers]',
        ])
        config.extend(f'{toml_string(k)} = {toml_string(v)}' for k, v in mcp.headers.items())
    path = codex / "config.toml"
    path.write_text("\n".join(config) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return cwd, env


async def terminate(proc: asyncio.subprocess.Process) -> None:
    # Always kill the group, including descendants surviving their parent.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    await asyncio.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


async def process(argv: list[str], cwd: Path, env: dict[str, str], stdin: bytes = b"",
                  *, jsonl: bool = False) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    size = 0
    messages: list[str] = []
    text_size = 0
    completed = False

    async def drain(stream, parse: bool) -> None:
        nonlocal size, text_size, completed
        pending = b""
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > MAX_OUTPUT:
                raise RunnerError("OUTPUT_LIMIT_EXCEEDED")
            if not parse:
                continue  # Never return or log stderr or tool stdout.
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    raise RunnerError("INVALID_CODEX_JSONL") from None
                if event.get("type") in {"turn.failed", "error"}:
                    raise RunnerError("CODEX_EXECUTION_FAILED")
                if event.get("type") == "turn.completed":
                    completed = True
                item = event.get("item")
                if (event.get("type") == "item.completed" and isinstance(item, dict)
                        and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                    text = item["text"]
                    text_size += len(text.encode("utf-8"))
                    if text_size > MAX_TEXT:
                        raise RunnerError("RESPONSE_LIMIT_EXCEEDED")
                    messages.append(text)
        if parse and pending.strip():
            raise RunnerError("INCOMPLETE_CODEX_JSONL")

    async def feed() -> None:
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    tasks = [asyncio.create_task(drain(proc.stdout, jsonl)),
             asyncio.create_task(drain(proc.stderr, False)), asyncio.create_task(feed())]
    try:
        await asyncio.gather(*tasks)
        if await proc.wait() != 0:
            raise RunnerError("CODEX_EXECUTION_FAILED")
        if jsonl and (not completed or not messages):
            raise RunnerError("CODEX_NO_FINAL_RESPONSE")
        return "\n\n".join(messages)
    finally:
        for task in tasks:
            task.cancel()
        await terminate(proc)
        await asyncio.gather(*tasks, return_exceptions=True)


async def execute(payload: Execute, key: str | None) -> str:
    executable = shutil.which("codex")
    if not executable:
        raise RunnerError("CODEX_CLI_NOT_INSTALLED", 503)
    # Entire lifecycle, including DNS and sandbox preflight, shares the deadline.
    with tempfile.TemporaryDirectory(prefix="agent-hub-run-") as directory:
        cwd, env = prepare(Path(directory), payload, key)
        try:
            with (oauth_session(Path(env["CODEX_HOME"])) if key is None
                  else contextlib.nullcontext()) as original:
                async with asyncio.timeout(TIMEOUT):
                    for mcp in payload.mcps:
                        await validate_remote(mcp)
                    if sys.platform == "linux":
                        # `codex sandbox` is platform-specific (no `linux` subcommand).
                        # This checks real enforcement without contacting a model.
                        probe = (
                            "import pathlib,socket,sys; "
                            "p=pathlib.Path('sandbox-write-probe'); "
                            "\ntry: p.write_text('forbidden')\nexcept OSError: pass\nelse: sys.exit(41)\n"
                            "s=socket.socket(); s.settimeout(1)\n"
                            "try: s.connect(('1.1.1.1',443))\nexcept OSError: pass\nelse: sys.exit(42)\n"
                        )
                        try:
                            await process([executable, "sandbox", "-c", 'sandbox_mode="read-only"',
                                           "--", sys.executable, "-c", probe], cwd, env)
                        except RunnerError:
                            raise RunnerError("SANDBOX_UNAVAILABLE", 503) from None
                    elif sys.platform != "darwin":
                        raise RunnerError("SANDBOX_PLATFORM_UNSUPPORTED", 503)
                    text = await process([
                        executable, "exec", "--strict-config", "--json", "--ephemeral", "--ignore-rules",
                        "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
                        "-C", str(cwd), "-",
                    ], cwd, env, payload.prompt.encode("utf-8"), jsonl=True)
                    secrets = [key] if key is not None else []
                    if key is None:
                        refreshed = read_oauth(Path(env["CODEX_HOME"]) / "auth.json")
                        for raw in (original, refreshed):
                            secrets.extend(json.loads(raw)["tokens"].values())
                    secrets.extend(v for m in payload.mcps for v in m.headers.values() if v)
                    for secret in (v for v in secrets if isinstance(v, str) and v):
                        text = text.replace(secret, "[REDACTED]")
                    return text
        except TimeoutError:
            raise RunnerError("RUN_TIMEOUT", 504) from None
        except OSError:
            raise RunnerError("RUNNER_PROCESS_ERROR", 503) from None


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.state.active = 0


@app.get("/health")
async def health():
    _, error = configured()
    if not error and not shutil.which("codex"):
        error = "CODEX_CLI_NOT_INSTALLED"
    return {"status": "not_ready" if error else "ready", "error": error, "codex_version": VERSION}


@app.post("/execute")
async def endpoint(request: Request):
    token = os.environ.get("RUNNER_TOKEN", "")
    if len(token.encode()) < 32:
        raise HTTPException(503, "RUNNER_TOKEN_NOT_CONFIGURED")
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, "UNAUTHORIZED")
    key, error = configured()
    if error:
        raise HTTPException(503, error)
    # No waiting queue: admission is atomic until the first await on this worker.
    # Deployment MUST use one uvicorn worker (Docker CMD below does).
    if app.state.active >= 2:
        raise HTTPException(429, "RUNNER_BUSY")
    app.state.active += 1
    work = watcher = None
    try:
        body = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise HTTPException(413, "REQUEST_TOO_LARGE")
        try:
            payload = Execute.model_validate_json(body)
        except ValidationError:
            # FastAPI's default 422 includes input values, potentially secrets.
            raise HTTPException(422, "INVALID_EXECUTE_PAYLOAD") from None

        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(0.1)

        work = asyncio.create_task(execute(payload, key))
        watcher = asyncio.create_task(disconnected())
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return {"text": work.result()}
        raise HTTPException(499, "CLIENT_DISCONNECTED")
    except RunnerError as exc:
        raise HTTPException(exc.status, exc.code) from None
    except TimeoutError:
        raise HTTPException(408, "REQUEST_TIMEOUT") from None
    finally:
        tasks = [t for t in (work, watcher) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        app.state.active -= 1
