import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('runner_personal_test', Path(__file__).with_name('main.py'))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
a = m.codex_auth
AUTH = json.dumps({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'private-access', 'refresh_token': 'private-refresh', 'id_token': 'private-id'}})


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv('RUNNER_TOKEN', 't' * 40)
    monkeypatch.setenv('HUB_AGENTS', 'codex')
    for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_BASE_URL', 'CODEX_MODEL', 'CODEX_AUTH_MODE', 'CODEX_PROXY_URL'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('value', ['https://evil.invalid/codex/device', 'http://auth.openai.com/codex/device',
                                  'https://auth.openai.com.evil.invalid/codex/device', 'https://auth.openai.com/codex/device#x'])
def test_only_exact_official_verification_link(value):
    assert not a.valid_link(value)


@pytest.mark.parametrize('raw', ['[]', '{}', 'null', 'x', json.dumps({'auth_mode': 'apikey', 'OPENAI_API_KEY': 'secret'})])
def test_credentials_require_chatgpt_tokens(raw):
    with pytest.raises((ValueError, TypeError)):
        a.credentials(raw)


def fake_cli(tmp_path, monkeypatch, outcome='success'):
    script = tmp_path / 'fake.py'
    script.write_text('''import json, os, sys
from pathlib import Path
for line in sys.stdin:
    q = json.loads(line)
    if 'id' not in q: continue
    method = q['method']
    result = {}
    if method == 'account/login/start':
        result = {'type':'chatgptDeviceCode','loginId':'login','verificationUrl':URL,'userCode':'ABCD-1234'}
        if OUTCOME == 'error':
            print(json.dumps({'id':q['id'],'error':{'message':'do-not-leak'}}),flush=True); continue
        if OUTCOME == 'success':
            Path(os.environ['CODEX_HOME'],'auth.json').write_text(AUTH)
            print(json.dumps({'method':'account/login/completed','params':{'loginId':'login','success':True}}),flush=True)
    if method == 'account/read': result = {'account':{'type':'chatgpt','email':'person@example.invalid','planType':'plus'}}
    print(json.dumps({'id':q['id'],'result':result}),flush=True)
'''.replace('URL', repr('https://evil.invalid' if outcome == 'bad_url' else a.VERIFY_URL)).replace('OUTCOME', repr(outcome)).replace('AUTH', repr(AUTH)))
    original = asyncio.create_subprocess_exec
    captured = []
    async def launch(*args, **kwargs):
        captured.append(kwargs)
        return await original(sys.executable, str(script), **kwargs)
    monkeypatch.setattr(a.asyncio, 'create_subprocess_exec', launch)
    return captured


@pytest.mark.parametrize('outcome,state', [('success', 'connected'), ('error', 'failed'), ('bad_url', 'failed')])
def test_structured_device_login_and_cleanup(tmp_path, monkeypatch, outcome, state):
    calls = fake_cli(tmp_path, monkeypatch, outcome)
    job = a.Login('job', 'unused', {})
    asyncio.run(job.run())
    assert job.state == state
    assert 'private-access' not in json.dumps(job.public())
    assert not Path(calls[0]['cwd']).exists()
    assert 'OPENAI_API_KEY' not in calls[0]['env']
    if state == 'connected':
        assert job.auth == AUTH and job.account['email'] == 'person@example.invalid'
        assert job.url is None and job.code is None
    else:
        assert job.auth is None and job.error == 'CODEX_DEVICE_AUTH_FAILED'


def test_cancel_pending_login(tmp_path, monkeypatch):
    calls = fake_cli(tmp_path, monkeypatch, 'pending')
    async def exercise():
        job = a.Login('job', 'unused', {})
        task = asyncio.create_task(job.run())
        for _ in range(100):
            if job.state == 'pending': break
            await asyncio.sleep(.01)
        assert job.state == 'pending'
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert job.state == 'cancelled' and job.code is None
        assert job.proc.returncode is not None
    asyncio.run(exercise())
    assert not Path(calls[0]['cwd']).exists()


def test_login_endpoints_authenticated_and_personal_mode_only(monkeypatch):
    monkeypatch.setattr(m.shutil, 'which', lambda _: '/bin/codex')
    with TestClient(m.app) as client:
        assert client.get('/codex-auth/mode').status_code == 401
        h = {'Authorization': 'Bearer ' + 't' * 40}
        assert client.get('/codex-auth/mode', headers=h).json()['mode'] == 'personal'
        monkeypatch.setenv('CODEX_BASE_URL', 'https://custom.example/v1')
        assert client.get('/codex-auth/mode', headers=h).json()['mode'] == 'unavailable'
        assert client.post('/codex-auth/login/' + 'a' * 36, headers=h).status_code == 409
        monkeypatch.setenv('OPENAI_API_KEY', 'key')
        assert client.get('/codex-auth/mode', headers=h).json()['mode'] == 'api'


def test_personal_session_saves_refresh_even_on_failure(tmp_path, monkeypatch):
    saved = []
    changed = AUTH.replace('private-refresh', 'rotated-refresh')
    async def bridge(payload, operation, auth=None):
        if operation == 'claim': return {'auth': AUTH}
        saved.append(auth)
        return {'ok': True}
    monkeypatch.setattr(m, 'credential_bridge', bridge)
    async def exercise():
        with pytest.raises(RuntimeError):
            async with m.personal_session(None, tmp_path):
                (tmp_path / 'auth.json').write_text(changed)
                raise RuntimeError('task failed')
    asyncio.run(exercise())
    assert saved == [changed]


def test_personal_config_does_not_inherit_custom_model(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_MODEL', 'third-party-model')
    p = m.Execute(run_id='a' * 36, conversation_id='c', prompt='test', codex_ticket='x' * 43)
    _, env = m.prepare(tmp_path, p, None)
    assert 'third-party-model' not in (Path(env['CODEX_HOME']) / 'config.toml').read_text()
    assert 'CODEX_API_KEY' not in env
    assert p.codex_ticket in m.request_secrets(p)


def test_manager_limits_pending_authorizations(monkeypatch):
    monkeypatch.setattr(a.shutil, 'which', lambda _: '/bin/codex')
    async def wait(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(a.Login, 'run', wait)
    async def exercise():
        manager = a.Manager()
        try:
            for i in range(3): await manager.start(str(i), {})
            assert (await manager.start('0', {}))['state'] == 'starting'
            with pytest.raises(HTTPException) as exc: await manager.start('four', {})
            assert exc.value.status_code == 429
        finally:
            for key in list(manager.jobs): await manager.cancel(key)
        assert manager.jobs == {}
    asyncio.run(exercise())


def test_personal_session_finishes_refresh_on_cancellation(tmp_path, monkeypatch):
    saved = []
    async def bridge(payload, operation, auth=None):
        if operation == 'claim': return {'auth': AUTH}
        await asyncio.sleep(.01)
        saved.append(auth)
        return {'ok': True}
    monkeypatch.setattr(m, 'credential_bridge', bridge)
    async def exercise():
        entered = asyncio.Event()
        async def task():
            async with m.personal_session(None, tmp_path):
                entered.set()
                await asyncio.Event().wait()
        work = asyncio.create_task(task())
        await entered.wait()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
    asyncio.run(exercise())
    assert saved == [AUTH]


def test_expired_login_is_cleared(tmp_path, monkeypatch):
    calls = fake_cli(tmp_path, monkeypatch, 'pending')
    monkeypatch.setattr(a, 'TTL', .05)
    job = a.Login('job', 'unused', {})
    asyncio.run(job.run())
    assert job.state == 'expired' and job.auth is None and job.code is None
    assert not Path(calls[0]['cwd']).exists()


@pytest.mark.parametrize('broken', [False, True])
def test_model_list_uses_signed_in_account_and_preserves_refreshed_auth(tmp_path, monkeypatch, broken):
    job = a.ModelQuery('models', 'fake', {}, AUTH)
    class Proc:
        returncode = 0
    async def launch(*args, **kwargs):
        assert 'OPENAI_API_KEY' not in kwargs['env']
        assert Path(kwargs['env']['CODEX_HOME'], 'auth.json').read_text() == AUTH
        return Proc()
    calls = []
    async def rpc(method, params):
        calls.append((method, params))
        if method == 'account/read':
            return {'account': {'type': 'chatgpt', 'email': 'test@example.invalid'}}
        if method == 'model/list':
            if broken:
                raise a.ProtocolError()
            if params.get('cursor') == 'next':
                return {'data': [{'model': 'model-b', 'displayName': 'Model B', 'hidden': False}], 'nextCursor': None}
            return {'data': [{'model': 'model-a', 'displayName': 'Model A', 'isDefault': True}, {'model': 'hidden', 'displayName': 'Hidden', 'hidden': True}], 'nextCursor': 'next'}
        return {}
    async def send(*args, **kwargs): pass
    monkeypatch.setattr(a.asyncio, 'create_subprocess_exec', launch)
    monkeypatch.setattr(job, 'rpc', rpc)
    monkeypatch.setattr(job, 'send', send)
    asyncio.run(job.run())
    assert job.state == 'connected' and job.auth == AUTH
    assert ('account/read', {'refreshToken': True}) in calls
    if broken:
        assert job.models == [] and job.error == 'CODEX_MODEL_QUERY_FAILED'
    else:
        assert [v['model'] for v in job.models] == ['model-a', 'model-b']
        assert job.models[0]['is_default'] is True


def test_selected_personal_model_applied_to_real_execution_config(tmp_path, monkeypatch):
    p = m.Execute(run_id='a' * 36, conversation_id='c', prompt='work', codex_ticket='x' * 43)
    _, env = m.prepare(tmp_path, p, None)
    async def bridge(payload, operation, auth=None):
        return {'auth': AUTH, 'model': 'model-from-account'} if operation == 'claim' else {'ok': True}
    monkeypatch.setattr(m, 'credential_bridge', bridge)
    async def run():
        async with m.personal_session(p, Path(env['CODEX_HOME'])):
            import tomllib
            config = tomllib.loads((Path(env['CODEX_HOME']) / 'config.toml').read_text())
            assert config['model'] == 'model-from-account'
    asyncio.run(run())
