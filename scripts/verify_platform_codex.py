"""Opt-in real Codex MCP check; isolated PG schema, no platform credentials/network login."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tests')]
from test_im_postgres import database
from app import platform_bridge, platform_auth, platform_broker, platform_worker
from app.models import User, Conversation, Message, Run, Audit, PlatformAuthRequest


def main():
    for line in (ROOT / '.env').read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            if k != 'DATABASE_URL':
                os.environ[k] = v
    os.environ['PLATFORM_BRIDGE_URL'] = 'http://127.0.0.1:18209/internal/platform-mcp'
    os.environ['CODEX_OAUTH_AUTH_FILE'] = str(ROOT / '.runtime/codex-oauth/auth.json')
    os.environ['PATH'] = str(ROOT / '.runtime/codex-cli/node_modules/.bin') + os.pathsep + os.environ['PATH']
    for provider in ('FEISHU', 'DINGTALK'):
        for field in ('CLIENT_ID', 'CLIENT_SECRET'):
            os.environ.pop('PLATFORM_' + provider + '_' + field, None)
    patch = pytest.MonkeyPatch()
    fixture = database.__wrapped__(patch)
    factory = next(fixture)
    app = FastAPI()
    app.include_router(platform_bridge.router)
    def session():
        with factory.begin() as db:
            yield db
    app.dependency_overrides[platform_bridge.get_db] = session
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=18209, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with factory.begin() as db:
            user = db.scalar(select(User))
            conv = Conversation(owner_id=user.id, title='isolated real Codex test')
            db.add(conv); db.flush()
            message = Message(conversation_id=conv.id, role='user', content='test')
            db.add(message); db.flush()
            run = Run(user_id=user.id, conversation_id=conv.id, message_id=message.id, status='running')
            db.add(run); db.flush()
            rid, cid, token = run.id, conv.id, platform_bridge.issue(run)
        for _ in range(50):
            if server.started:
                break
            time.sleep(.1)
        spec = importlib.util.spec_from_file_location('platform_real_runner', ROOT / 'runner/main.py')
        runner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = runner
        spec.loader.exec_module(runner)
        payload = runner.Execute(run_id=rid, conversation_id=cid, platform_capability=token,
            prompt='用户明确请求隔离工具验证：实际调用 get_platform_authorization_status(provider=feishu)，然后实际调用 request_platform_authorization(provider=dingtalk)。无应用配置预期 setup_required；不模拟调用。最后只报告状态。')
        result = asyncio.run(runner.execute(payload, None))
        print('Codex final:', result)
        with factory() as db:
            calls = [a.details for a in db.scalars(select(Audit).where(Audit.action == 'platform.tool_call'))]
            print('Verified server tool calls:', calls)
            assert {a['tool'] for a in calls} == set(platform_bridge.TOOLS)
        print('REAL_CODEX_BRIDGE_PASS; isolated PG; no external platform login')
        # Exercise the real Codex tool again with only isolated fake issuer/IM.
        from test_platform_auth import device
        # Personal OAuth must be the same app as the isolated bot identity ('app' in the test fixtures).
        patch.setenv('PLATFORM_FEISHU_CLIENT_ID', 'app')
        patch.setenv('PLATFORM_FEISHU_CLIENT_SECRET', 'secret')
        patch.setattr(platform_auth, 'begin', device)
        sent = []
        patch.setattr(platform_broker, 'dispatch', lambda *args: sent.append(args))
        with factory.begin() as db:
            from app.im import _enqueue
            _enqueue(db, 'feishu', 'isolated-real-codex', 'sender', 'chat', '发起本人飞书授权', True)
            run = db.scalar(select(Run).where(Run.id != rid))
            run.status = 'running'
            mock_run, mock_conversation, capability = run.id, run.conversation_id, platform_bridge.issue(run)
        result = asyncio.run(runner.execute(runner.Execute(run_id=mock_run, conversation_id=mock_conversation,
            platform_capability=capability, prompt='本次用户明确要求发起本人飞书授权。必须实际调用 request_platform_authorization(provider=feishu)，再简短报告工具返回的投递状态。'), None))
        platform_worker.tick(factory)
        platform_worker.tick(factory)
        assert len(sent) == 1 and sent[0][1] == 'sender'
        assert 'secret-code' not in result and 'accounts.feishu.cn' not in result
        with factory() as db:
            assert db.scalar(select(PlatformAuthRequest.id).where(PlatformAuthRequest.run_id == mock_run))
            assert all('secret-code' not in m.content for m in db.scalars(select(Message)))
        print('REAL_CODEX_MOCK_PRIVATE_DELIVERY_PASS; no external IM')
    finally:
        server.should_exit = True
        thread.join(5)
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()


if __name__ == '__main__':
    main()
