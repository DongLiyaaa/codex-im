import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from fastapi import HTTPException

TTL = 600
LIMIT = 65536
VERIFY_URL = 'https://auth.openai.com/codex/device'


def valid_link(value):
    return isinstance(value, str) and value == VERIFY_URL


def credentials(raw):
    if not isinstance(raw, str) or len(raw.encode()) > LIMIT:
        raise ValueError('INVALID_CREDENTIALS')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('INVALID_CREDENTIALS')
    tokens = data.get('tokens')
    if (data.get('auth_mode') != 'chatgpt' or data.get('OPENAI_API_KEY') or not isinstance(tokens, dict)
            or not all(isinstance(tokens.get(k), str) and tokens[k] for k in ('access_token', 'refresh_token', 'id_token'))):
        raise ValueError('INVALID_CREDENTIALS')
    return raw


class ProtocolError(Exception):
    pass


class Login:
    def __init__(self, identifier, executable, proxy):
        self.identifier, self.executable, self.proxy = identifier, executable, proxy
        self.state, self.error = 'starting', None
        self.url = self.code = self.account = self.auth = None
        self.login_id = None
        self.deadline = time.monotonic() + TTL
        self.task = self.proc = None
        self.sequence = 0
        self.completed = []

    def public(self):
        return {'state': self.state, 'error': self.error, 'url': self.url, 'user_code': self.code,
                'expires_in': max(0, int(self.deadline - time.monotonic()))}

    async def send(self, method, params=None, request=True):
        message = {'method': method}
        if params is not None:
            message['params'] = params
        if request:
            self.sequence += 1
            message['id'] = self.sequence
        self.proc.stdin.write((json.dumps(message) + '\n').encode())
        await self.proc.stdin.drain()
        return self.sequence

    async def receive(self):
        line = await self.proc.stdout.readline()
        if not line or len(line) > LIMIT:
            raise ProtocolError()
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ProtocolError()
        if message.get('method') == 'account/login/completed':
            if len(self.completed) >= 8:
                raise ProtocolError()
            self.completed.append(message.get('params') or {})
        return message

    async def rpc(self, method, params=None):
        identifier = await self.send(method, params)
        for _ in range(100):
            message = await self.receive()
            if message.get('id') == identifier:
                if 'error' in message:
                    raise ProtocolError()
                result = message.get('result')
                if not isinstance(result, dict):
                    raise ProtocolError()
                return result
        raise ProtocolError()

    async def run(self):
        try:
            with tempfile.TemporaryDirectory(prefix='hub-codex-login-') as directory:
                root = Path(directory)
                env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': directory,
                       'CODEX_HOME': directory, 'LANG': 'C.UTF-8', 'NO_COLOR': '1', **self.proxy}
                (root / 'config.toml').write_text('cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n')
                async with asyncio.timeout(TTL):
                    self.proc = await asyncio.create_subprocess_exec(self.executable, 'app-server', cwd=directory, env=env,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        limit=LIMIT, start_new_session=True)
                    try:
                        async with asyncio.timeout(30):
                            await self.rpc('initialize', {'clientInfo': {'name': 'agent_hub', 'title': 'Agent Hub', 'version': '0.0.4'}})
                            await self.send('initialized', request=False)
                            result = await self.rpc('account/login/start', {'type': 'chatgptDeviceCode'})
                        if (result.get('type') != 'chatgptDeviceCode' or not valid_link(result.get('verificationUrl'))
                                or not isinstance(result.get('loginId'), str) or len(result['loginId']) > 128
                                or not re.fullmatch(r'[A-Za-z0-9-]{4,32}', result.get('userCode', ''))):
                            raise ProtocolError()
                        self.login_id = result['loginId']
                        self.url, self.code, self.state = result['verificationUrl'], result['userCode'], 'pending'
                        while True:
                            completion = next((v for v in self.completed if v.get('loginId') == self.login_id), None)
                            if completion is not None:
                                if completion.get('success') is not True:
                                    raise ProtocolError()
                                break
                            await self.receive()
                        account = (await self.rpc('account/read', {'refreshToken': False})).get('account')
                        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
                            raise ProtocolError()
                        path = root / 'auth.json'
                        if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
                            raise ProtocolError()
                        path.chmod(0o600)
                        self.auth = credentials(path.read_text())
                        self.account = {k: v[:200] for k in ('email', 'planType') if isinstance((v := account.get(k)), str)}
                        self.state, self.code, self.url = 'connected', None, None
                    finally:
                        await self.terminate()
        except asyncio.CancelledError:
            self.state, self.auth, self.code, self.url = 'cancelled', None, None, None
            raise
        except TimeoutError:
            self.state, self.error = 'expired', 'AUTHORIZATION_EXPIRED'
        except (OSError, ValueError, TypeError, AttributeError, ProtocolError, RuntimeError):
            self.state, self.error = 'failed', 'CODEX_DEVICE_AUTH_FAILED'
        finally:
            if self.state != 'connected':
                self.auth = self.code = self.url = None

    async def terminate(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()


class Manager:
    def __init__(self):
        self.jobs = {}

    async def prune(self):
        for identifier, job in list(self.jobs.items()):
            if time.monotonic() >= job.deadline:
                await self.cancel(identifier)

    async def start(self, identifier, proxy):
        await self.prune()
        if identifier in self.jobs:
            return self.jobs[identifier].public()
        if len(self.jobs) >= 100 or sum(not j.task.done() for j in self.jobs.values()) >= 3:
            raise HTTPException(429, 'CODEX_LOGIN_BUSY')
        executable = shutil.which('codex')
        if not executable:
            raise HTTPException(503, 'CODEX_CLI_NOT_INSTALLED')
        job = Login(identifier, executable, proxy)
        self.jobs[identifier] = job
        job.task = asyncio.create_task(job.run())
        asyncio.get_running_loop().call_later(TTL, lambda: asyncio.create_task(self.cancel(identifier)))
        return job.public()

    async def cancel(self, identifier):
        job = self.jobs.pop(identifier, None)
        if job:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            job.auth = job.code = job.url = None

    async def read(self, identifier, result=False):
        await self.prune()
        job = self.jobs.get(identifier)
        if not job:
            raise HTTPException(404, 'CODEX_LOGIN_INTERRUPTED')
        return {**job.public(), **({'auth': job.auth, 'account': job.account} if result and job.state == 'connected' else {})}
