"""Home-page check of the Codex CLI integration.

The API never runs Codex itself, the runner does, so the truth about "is Codex connected" lives there. This reads the
runner's token-protected /status on behalf of a super administrator and returns a fixed set of facts: whatever else
the runner (or a stand-in answering on its address) might send is dropped, and every failure is reduced to a static
code, so no upstream text, URL or credential can reach the page.
"""
import os
from typing import Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError

from . import policy
from .security import current_user

router = APIRouter(prefix='/api/system', tags=['system'])
LIMIT = 64 * 1024
# Longer than the runner's own deadline, so the runner's answer (or its static timeout code) wins.
TIMEOUT = httpx.Timeout(35.0, connect=5.0)
STATUS_ERRORS = {401: 'RUNNER_AUTH_FAILED', 404: 'RUNNER_OUTDATED', 503: 'RUNNER_TOKEN_NOT_CONFIGURED', 504: 'RUNNER_TIMEOUT'}


class Codex(BaseModel):
    installed: bool
    version: str | None = Field(None, max_length=40)
    pinned_version: str = Field(max_length=40)


class Model(BaseModel):
    id: str | None = Field(None, max_length=100)
    endpoint_host: str | None = Field(None, max_length=253)
    credential_configured: bool


class Sandbox(BaseModel):
    state: Literal['ok', 'failed', 'skipped']
    error: str | None = Field(None, max_length=60)


class Endpoint(BaseModel):
    state: Literal['ok', 'model_missing', 'unauthorized', 'timeout', 'unreachable', 'http_error', 'redirect_refused',
                   'invalid_response', 'unverified', 'skipped']
    http_status: int | None = Field(None, ge=100, le=599)
    model_listed: bool | None = None
    reason: str | None = Field(None, max_length=60)


class AgentFlag(BaseModel):
    enabled: bool
    ready: bool


class ClaudeStatus(BaseModel):
    enabled: bool
    ready: bool
    installed: bool = False
    version: str | None = Field(None, max_length=40)
    pinned_version: str | None = Field(None, max_length=40)
    model: Model | None = None
    config_error: str | None = Field(None, max_length=60)
    model_endpoint: Endpoint | None = None


class Agents(BaseModel):
    codex: AgentFlag
    claude: ClaudeStatus


class RunnerStatus(BaseModel):
    ready: bool
    checked_at: int
    auth_mode: Literal['api', 'chatgpt', 'invalid']
    codex: Codex
    model: Model
    config_error: str | None = Field(None, max_length=60)
    sandbox: Sandbox
    model_endpoint: Endpoint
    # Absent when talking to a runner from before Claude CLI support: Codex-only, nothing else to show.
    agents: Agents | None = None


def failure(code):
    return {'reachable': False, 'error': code, 'runner': None}


def check(refresh=False):
    token = os.getenv('RUNNER_TOKEN', '')
    if not token:
        return failure('RUNNER_TOKEN_NOT_CONFIGURED')
    base = os.getenv('RUNNER_URL', 'http://127.0.0.1:18201').rstrip('/')
    try:
        with httpx.Client(base_url=base, timeout=TIMEOUT, follow_redirects=False, trust_env=False) as client:
            with client.stream('GET', '/status', params={'refresh': 'true'} if refresh else None,
                               headers={'Authorization': f'Bearer {token}'}) as response:
                if response.status_code != 200:
                    return failure(STATUS_ERRORS.get(response.status_code, 'RUNNER_ERROR'))
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > LIMIT:
                        return failure('RUNNER_INVALID_RESPONSE')
    except httpx.TimeoutException:
        return failure('RUNNER_TIMEOUT')
    except (httpx.HTTPError, httpx.InvalidURL):
        return failure('RUNNER_UNREACHABLE')
    try:
        status = RunnerStatus.model_validate_json(body)
    except ValidationError:
        return failure('RUNNER_INVALID_RESPONSE')
    return {'reachable': True, 'error': None,
            'runner': status.model_dump(exclude={'agents'} if status.agents is None else None)}


@router.get('/codex-status')
def codex_status(refresh: bool = False, actor=Depends(current_user)):
    # The result names the model and endpoint host, so it is for the platform administrator only.
    policy.require(actor.active and actor.role == 'super_admin')
    return check(refresh)
