"""Fixed internal MCP service. Bearer capabilities never accept a model user ID."""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from .db import get_db
from .security import current_user
from .models import Run, User
from . import platform_auth

router = APIRouter()
TOOLS = ['get_platform_authorization_status', 'request_platform_authorization']


def key():
    value = os.getenv('PLATFORM_BRIDGE_KEY', '')
    if len(value) < 32:
        raise HTTPException(503, 'Platform bridge unavailable')
    return value.encode()


def issue(run):
    raw = json.dumps({'run': run.id, 'user': run.user_id, 'conversation': run.conversation_id,
                      'exp': int(time.time()) + 240, 'aud': 'platform-authorization'}, separators=(',', ':')).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return encoded + '.' + hmac.new(key(), encoded.encode(), hashlib.sha256).hexdigest()


def verify(token, db):
    try:
        encoded, signature = token.split('.')
        if not hmac.compare_digest(signature, hmac.new(key(), encoded.encode(), hashlib.sha256).hexdigest()):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if claims['exp'] <= time.time() or claims['exp'] > time.time() + 250 or claims['aud'] != 'platform-authorization':
            raise ValueError()
        run = db.get(Run, claims['run'])
        if not run or run.status != 'running' or run.user_id != claims['user'] or run.conversation_id != claims['conversation']:
            raise ValueError()
        from .service import build_payload
        build_payload(db, run)  # Includes active membership, IM mapping and group policy checks.
        return db.get(User, run.user_id)
    except Exception:
        raise HTTPException(403, 'Invalid or expired platform capability') from None


@router.get('/api/platform-connections')
def connections(actor=Depends(current_user), db=Depends(get_db)):
    return [platform_auth.operate(db, actor, p, private=True) for p in platform_auth.PROVIDERS]


@router.post('/api/platform-connections/{provider}/{action}')
def change(provider: str, action: str, actor=Depends(current_user), db=Depends(get_db)):
    if action not in ('start', 'refresh', 'cancel', 'disconnect'):
        raise HTTPException(404, 'Not found')
    if action == 'start':
        from .security import rate_limit
        rate_limit('platform-start:' + actor.id + ':' + provider)
    return platform_auth.operate(db, actor, provider, action, private=True)


@router.get('/api/integrations/oauth/{provider}')
def oauth_config(provider: str, actor=Depends(current_user), db=Depends(get_db)):
    from . import policy, platform_settings
    policy.require(actor.active and actor.role == 'super_admin')
    return platform_settings.view(db, provider)


from .platform_settings import Update as OAuthUpdate


@router.put('/api/integrations/oauth/{provider}')
def oauth_save(provider: str, body: OAuthUpdate, actor=Depends(current_user), db=Depends(get_db)):
    from . import policy, platform_settings
    from .service import audit
    policy.require(actor.active and actor.role == 'super_admin')
    result = platform_settings.save(db, provider, body)
    audit(db, actor, 'platform.configuration.update', provider, {'revision': result['revision']})
    return result


@router.post('/internal/platform-mcp')
async def mcp(request: Request, db=Depends(get_db)):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer ') or len(auth) > 2048:
        raise HTTPException(403, 'Invalid capability')
    actor = verify(auth[7:], db)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, 'Request too large')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
            raise ValueError()
    except Exception:
        raise HTTPException(400, 'Invalid MCP request') from None
    method, params = body.get('method'), body.get('params', {})
    if method == 'notifications/initialized':
        return Response(status_code=202)
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'hub-personal-platforms', 'version': '1.0.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'description': '查询本人平台授权状态' if i == 0 else '按当前任务需要发起本人授权。后台私发官方材料或提供本人卡片；仅返回脱敏投递状态，完成后须重发任务。',
            'annotations': {'readOnlyHint': i == 0, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
            'inputSchema': {'type': 'object', 'properties': {'provider': {'type': 'string', 'enum': list(platform_auth.PROVIDERS)}},
                            'required': ['provider'], 'additionalProperties': False}} for i, name in enumerate(TOOLS)]}
    elif method == 'tools/call':
        if not isinstance(params, dict) or params.get('name') not in TOOLS:
            raise HTTPException(400, 'Unknown tool')
        args = params.get('arguments', {})
        if not isinstance(args, dict) or set(args) != {'provider'} or args['provider'] not in platform_auth.PROVIDERS:
            raise HTTPException(400, 'Invalid tool arguments')
        from .service import audit
        audit(db, actor, 'platform.tool_call', args['provider'], {'tool': params['name']})
        from .platform_broker import operate
        claims = json.loads(base64.urlsafe_b64decode(auth[7:].split('.')[0] + '=' * (-len(auth[7:].split('.')[0]) % 4)))
        run = db.get(Run, claims['run'])
        status = operate(db, actor, args['provider'], 'start' if params['name'] == TOOLS[1] else 'status', run=run)
        status['entry_message'] = ('授权材料已私发本人，请完成官方授权后重发任务。' if status['delivery_status'] == 'delivered' else
            '授权材料将在后台私发本人，群聊不会显示设备码。' if status['delivery_status'] in ('queued', 'sending') else
            '请在当前 Hub 会话打开仅本人可见的授权卡片；IM 需要该平台本人 Identity 绑定与支持私发的应用配置。')
        origin = os.getenv('APP_ORIGIN', '').rstrip('/')
        parsed = urlsplit(origin)
        try:
            from .service import validate_mcp_url
            validate_mcp_url(origin)
            public_entry = not parsed.path.strip('/') and not parsed.query
        except HTTPException:
            public_entry = False
        if public_entry:
            status['personal_connection_page'] = origin + '/#/connections'
        else:
            status['hub_entry_message'] = 'Hub 尚无远端员工可达的 HTTPS 入口；不会向 IM 提供 localhost 链接。'
        result = {'content': [{'type': 'text', 'text': json.dumps(status, ensure_ascii=False)}], 'isError': False}
    else:
        return {'jsonrpc': '2.0', 'id': body.get('id'), 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}
