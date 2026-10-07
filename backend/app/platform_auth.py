"""Personal device authorization; protocol pinned to lark-cli 1.0.96 / dws 1.0.62.

No CLI process, host credentials, tenant token, dynamic endpoint or log parsing.
Device material and user credentials exist only in encrypted PostgreSQL rows.
"""
import base64
import hashlib
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select, text

from .models import PlatformConnection, now

PROVIDERS = ('feishu', 'dingtalk')
# Feishu: the whole cloud-document domain (docs, sheets, bases, whiteboards, drive, wiki, doc search),
# matching what run_platform_command can reach. Deliberately excludes mail, calendar, contacts, approval,
# tasks and IM, so a steered model cannot act as the user outside documents. Every scope here must be
# enabled on the app as a user scope, otherwise Feishu rejects the device authorization request.
SCOPES = {'feishu': ' '.join([
    'docx:document', 'docx:document:create', 'docx:document:readonly', 'docx:document:write_only',
    'docs:document.content:read', 'docs:document.comment:create', 'docs:document.comment:delete',
    'docs:document.comment:read', 'docs:document.comment:update', 'docs:document.comment:write_only',
    'docs:document.media:download', 'docs:document.media:upload', 'docs:document:copy', 'docs:document:export',
    'docs:document:import', 'docs:permission.member:apply', 'docs:permission.member:auth',
    'docs:permission.member:create', 'docs:permission.member:transfer', 'docs:permission.setting:read',
    'docs:permission.setting:write_only', 'docs:secure_label:write_only',
    'sheets:spreadsheet', 'sheets:spreadsheet:create', 'sheets:spreadsheet:read', 'sheets:spreadsheet:write_only',
    'sheets:spreadsheet.meta:read', 'sheets:spreadsheet.meta:write_only',
    'base:app:copy', 'base:app:create', 'base:app:read', 'base:app:update',
    'base:dashboard:create', 'base:dashboard:delete', 'base:dashboard:read', 'base:dashboard:update',
    'base:field:create', 'base:field:delete', 'base:field:read', 'base:field:update',
    'base:form:create', 'base:form:delete', 'base:form:read', 'base:form:update', 'base:history:read',
    'base:record:create', 'base:record:delete', 'base:record:read', 'base:record:retrieve', 'base:record:update',
    'base:role:create', 'base:role:delete', 'base:role:read', 'base:role:update',
    'base:table:create', 'base:table:delete', 'base:table:read', 'base:table:update', 'base:view:read',
    'base:view:write_only', 'base:workflow:create', 'base:workflow:read', 'base:workflow:update',
    'board:whiteboard:node:create', 'board:whiteboard:node:read',
    'drive:drive', 'drive:drive.metadata:readonly', 'drive:drive:version', 'drive:file:download', 'drive:file:upload',
    'drive:file:view_record:readonly', 'drive:quota_detail:read_one',
    'space:document:delete', 'space:document:move', 'space:document:retrieve', 'space:document:shortcut',
    'space:folder:create',
    'wiki:member:create', 'wiki:member:retrieve', 'wiki:member:update', 'wiki:node:copy', 'wiki:node:create',
    'wiki:node:move', 'wiki:node:read', 'wiki:node:retrieve', 'wiki:space:read', 'wiki:space:retrieve',
    'wiki:space:write_only', 'search:docs:read']),
    'dingtalk': 'openid corpid'}
# Granted scopes may be narrower than requested (e.g. tenant review); documents must at least be writable.
REQUIRED = {'feishu': {'docx:document'}}
# A token carrying any of these domains could act as the user outside documents; never accept it.
FORBIDDEN_SCOPES = ('im:', 'mail:', 'calendar:', 'contact:', 'approval:', 'task:', 'search:message')
# RFC 8628 device-flow polling answers; Feishu delivers them with HTTP 400, so they are not failures.
POLL_ANSWERS = ('authorization_pending', 'slow_down')
HOSTS = {'feishu': {'accounts.feishu.cn', 'open.feishu.cn'},
         'dingtalk': {'login.dingtalk.com', 'open-dev.dingtalk.com'}}
SETUP = '个人 OAuth 应用尚未配置，请超级管理员在 IM 集成中独立配置，或确认使用当前机器人应用；请勿在聊天中粘贴密钥。设备授权无需 Hub 公网或回调。'

MESSAGES = {
    'configuration_missing': (SETUP, 'configure_application'),
    'configuration_changed': ('机器人应用配置已变化，旧复制快照已停用；请管理员重新确认或配置独立应用。', 'reconfirm_application'),
    'provider_not_supported': ('平台不支持当前应用的设备授权。请管理员核对官方应用能力；本版不代理外部回调，也不会伪造授权链接。', 'check_provider_capability'),
    'provider_denied': ('平台或本人拒绝了授权，请核对用户权限后由本人重新发起。', 'review_provider_permissions'),
    'provider_invalid_config': ('平台拒绝应用凭据或权限配置，请管理员在网页核对应用设置；勿在聊天中发送密钥。', 'configure_application'),
    'organization_denied': ('钉钉组织尚未允许本人或当前渠道使用 CLI 数据访问，请联系组织管理员；Hub 不会代修改组织权限。', 'contact_organization_admin'),
    'identity_missing': ('缺少目标平台当前应用下的本人身份绑定，请管理员明确绑定后重试；不能借用消息来源平台身份。', 'bind_target_identity'),
    'identity_app_mismatch': ('个人 OAuth 与机器人不是同一应用，无法安全核验应用范围内的本人身份；请管理员核对配置。', 'configure_application'),
    'private_delivery_unsupported': ('钉钉旧 webhook 不支持私发本人授权材料，请管理员配置 Stream 应用后重试。', 'configure_private_delivery'),
    'private_delivery_failed': ('授权材料私发失败，本次材料已清除；请检查机器人私发权限及本人绑定后重新发起。', 'check_private_delivery'),
    'starting': ('后台正在发起官方设备授权，仅向官方服务出站 HTTPS。', 'wait'),
    'pending': ('等待本人完成官方授权，后台轮询确认；完成后请重新发送任务。', 'approve_personally'),
    'connected': ('本人授权已连接，可以以你本人身份创建、读取和编辑云文档/表格/多维表格；请重新发送任务。', 'resend_task'),
    'identity_mismatch': ('授权平台身份与当前绑定的本人身份不符，凭据已丢弃。', 'verify_target_identity'),
    'interrupted': ('授权过程已中断。为避免重复交换或重复私发，请重新发起。', 'retry_authorization'),
    'platform_rejected': ('平台设备授权响应异常或暂不可用，请稍后重试；不是 Hub 公网配置问题。', 'retry_authorization'),
    'expired': ('本人授权已过期或应用/身份配置变化，请重新发起。', 'retry_authorization'),
    'disconnected': ('本人平台尚未连接。', 'request_authorization'),
}


class ProviderError(ValueError):
    def __init__(self, state, reason=None):
        # reason is a short machine code shown to administrators; it never carries upstream text or credentials.
        self.state, self.reason = state, reason
        super().__init__(state)


def scope_problem(granted):
    if not REQUIRED['feishu'].issubset(granted):
        return 'SCOPE_NOT_WRITABLE'
    domains = sorted({s.split(':')[0] for s in granted if s.startswith(FORBIDDEN_SCOPES)})
    return ('SCOPE_OUT_OF_DOMAIN:' + ','.join(domains))[:64] if domains else None


def provider_error(data):
    # Only classify machine codes; never return upstream messages or private bodies.
    code = str(data.get('error') or data.get('errorCode') or data.get('code') or '').lower()
    if code in ('unsupported_grant_type', 'unauthorized_client', 'unsupported_client', 'device_flow_not_supported'):
        return 'provider_not_supported'
    if code in ('invalid_client', 'invalid_scope', 'invalid_request', 'invalid_client_secret'):
        return 'provider_invalid_config'
    if code in ('access_denied', 'authorization_declined', 'rejected', 'forbidden'):
        return 'provider_denied'
    if code in ('expired_token', 'invalid_grant'):
        return 'expired'
    return 'platform_rejected'


def check_response(data):
    if data.get('error') or data.get('success') is False:
        raise ProviderError(provider_error(data))


def check_provider(provider):
    if provider not in PROVIDERS:
        raise HTTPException(404, 'Unknown platform')


def credentials(provider):
    check_provider(provider)
    from .platform_settings import values
    configured = values(provider)
    return configured['CLIENT_ID'], configured['CLIENT_SECRET']


def cipher():
    secret = os.getenv('PLATFORM_AUTH_KEY', '')
    if secret:
        return Fernet(secret.encode())
    secret = os.getenv('SESSION_SECRET', '')
    if len(secret) < 32:
        raise HTTPException(503, 'Platform encryption key unavailable')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'agent-hub/platform/v1\0' + secret.encode()).digest()))


def seal(data):
    return cipher().encrypt(json.dumps(data).encode()).decode()


def unseal(row):
    if not row.encrypted:
        return {}
    return json.loads(cipher().decrypt(row.encrypted.encode()))


def valid_link(provider, value):
    try:
        u = urlsplit(value)
        return (len(value) <= 8192 and u.scheme == 'https' and u.hostname in HOSTS[provider]
                and not u.username and not u.password and not u.fragment and u.port in (443, None)
                and not any(ord(c) < 33 or ord(c) == 127 for c in value))
    except (ValueError, TypeError):
        return False


ENDPOINT_HOSTS = frozenset(('accounts.feishu.cn', 'open.feishu.cn', 'login.dingtalk.com', 'api.dingtalk.com', 'mcp.dingtalk.com', 'oapi.dingtalk.com'))


def request(method, url, **kwargs):
    # Literal official hosts only; resolve once, pin TLS to a validated public IP.
    from .attachment_download import resolve_addresses, PinnedHTTPS
    from urllib.parse import urlencode
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ENDPOINT_HOSTS or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('invalid_endpoint')
    headers = dict(kwargs.get('headers', {}))
    body = None
    if 'json' in kwargs:
        body = json.dumps(kwargs['json']).encode()
        headers['Content-Type'] = 'application/json'
    elif 'data' in kwargs:
        body = urlencode(kwargs['data']).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    if kwargs.get('auth'):
        headers['Authorization'] = 'Basic ' + base64.b64encode(':'.join(kwargs['auth']).encode()).decode()
    path = parsed.path or '/'
    query = urlencode(kwargs['params']) if kwargs.get('params') else parsed.query
    connection = PinnedHTTPS(parsed.hostname, resolve_addresses(parsed.hostname)[0])
    connection.timeout = 12
    try:
        connection.request(method, path + ('?' + query if query else ''), body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('response_limit')
        if response.status >= 500 or 300 <= response.status < 400:
            raise ValueError('upstream_unavailable')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid_response')
        if response.status >= 400:
            if result.get('error') in POLL_ANSWERS:
                return result
            raise ProviderError(provider_error(result))
        return result
    finally:
        connection.close()



def begin(provider):
    client_id, secret = credentials(provider)
    if provider == 'feishu':
        data = request('POST', 'https://accounts.feishu.cn/oauth/v1/device_authorization',
                       data={'client_id': client_id, 'scope': SCOPES[provider]}, auth=(client_id, secret))
        check_response(data)
        result = {k: data.get(k) for k in ('device_code', 'user_code', 'expires_in', 'interval')}
        result['url'] = data.get('verification_uri_complete') or data.get('verification_uri')
    else:
        data = request('POST', 'https://login.dingtalk.com/oauth2/device/code.json',
                       data={'client_id': client_id, 'scope': SCOPES[provider]})
        check_response(data)
        if data.get('success') is not True:
            raise ProviderError('platform_rejected')
        d = data['result']
        result = {'device_code': d.get('deviceCode'), 'user_code': d.get('userCode'),
                  'url': d.get('verificationUriComplete') or d.get('verificationUri'),
                  'expires_in': d.get('expiresIn'), 'interval': d.get('interval'), 'flow_id': d.get('flowId')}
    if (not valid_link(provider, result['url']) or not isinstance(result['device_code'], str)
            or not result['device_code'] or len(result['device_code']) > 8192
            or not isinstance(result['user_code'], str) or not 1 <= len(result['user_code']) <= 128
            or any(ord(c) < 33 or ord(c) == 127 for c in result['user_code'])):
        raise ValueError('invalid_device_response')
    if type(result['expires_in']) is not int or result['expires_in'] <= 0 or type(result['interval']) is not int or result['interval'] <= 0:
        raise ValueError('invalid_device_response')
    result['expires_in'] = min(result['expires_in'], 900)
    # Never poll faster than the issuer interval, even if it exceeds our expiry.
    result['interval'] = max(5, result['interval'])
    result['client_fingerprint'] = hashlib.sha256((client_id + '\0' + secret).encode()).hexdigest()
    return result


def poll(provider, device):
    client_id, secret = credentials(provider)
    if device['client_fingerprint'] != hashlib.sha256((client_id + '\0' + secret).encode()).hexdigest():
        return 'expired', {}
    if provider == 'feishu':
        data = request('POST', 'https://open.feishu.cn/open-apis/authen/v2/oauth/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device['device_code'],
            'client_id': client_id, 'client_secret': secret})
        if data.get('error') in ('authorization_pending', 'slow_down'):
            return data['error'], {}
        check_response(data)
        if not data.get('access_token'):
            raise ProviderError('platform_rejected')
        granted = set(str(data.get('scope') or '').split())
        # Must be able to write documents, and must never carry another domain's power (mail, IM, calendar...).
        # Harmless baseline scopes the platform adds on its own (offline_access, user identity) are tolerated.
        problem = scope_problem(granted)
        if problem:
            raise ProviderError('provider_invalid_config', problem)
        return 'connected', {'access_token': data['access_token'], 'expires_in': data.get('expires_in', 7200),
                             'scope': ' '.join(sorted(granted))}
    if device.get('flow_id'):
        data = request('GET', 'https://mcp.dingtalk.com/cli/oauth/device/poll', params={'flowId': device['flow_id']})
        result = data.get('data') if (data.get('data') or {}).get('status') else data.get('result') or {}
        status = result.get('status')
        if status == 'PENDING':
            return 'authorization_pending', {}
        if status == 'REJECTED':
            return 'provider_denied', {}
        if status == 'EXPIRED':
            return 'expired', {}
        check_response(data)
        if status != 'APPROVED':
            raise ProviderError('platform_rejected')
        code = result.get('authCode')
    else:
        data = request('POST', 'https://login.dingtalk.com/oauth2/device/token.json', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device['device_code'], 'client_id': client_id})
        result = data.get('result') or {}
        if result.get('error') in ('authorization_pending', 'slow_down'):
            return result['error'], {}
        check_response(data)
        if result.get('error'):
            raise ProviderError(provider_error(result))
        code = result.get('authCode')
    if not code:
        raise ProviderError('platform_rejected')
    data = request('POST', 'https://api.dingtalk.com/v1.0/oauth2/userAccessToken', json={
        'clientId': client_id, 'clientSecret': secret, 'code': code, 'grantType': 'authorization_code'})
    check_response(data)
    token = data.get('accessToken')
    if not token:
        raise ProviderError(provider_error(data))
    allowed = request('GET', 'https://mcp.dingtalk.com/cli/cliAuthEnabled', headers={'x-user-access-token': token})
    if allowed.get('success') is not True or (allowed.get('result') or {}).get('cliAuthEnabled') is not True:
        return 'organization_denied', {}
    return 'connected', {'access_token': token, 'expires_in': data.get('expiresIn', 7200)}


def locked(db, user_id, provider):
    check_provider(provider)
    # Transaction-scoped cross-process serialization also covers first insert.
    key = int.from_bytes(hashlib.sha256((user_id + ':' + provider).encode()).digest()[:8], 'big', signed=True)
    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
    row = db.get(PlatformConnection, (user_id, provider))
    if row is None:
        row = PlatformConnection(user_id=user_id, provider=provider, state='disconnected', encrypted='')
        db.add(row)
        db.flush()
    if row.expires_at and row.expires_at <= now() and row.state in ('pending', 'connected'):
        row.state, row.encrypted = 'expired', ''
    return row


def view(row, private=False):
    configured = all(credentials(row.provider))
    state = row.state if configured else 'configuration_missing'
    if state == 'setup_required':
        state = 'configuration_missing'
    message, next_action = MESSAGES.get(state, MESSAGES['platform_rejected'])
    output = {'provider': row.provider, 'state': state, 'next_action': next_action,
              'scope': SCOPES[row.provider], 'expires_at': row.expires_at.isoformat() if row.expires_at else None,
              'message': message}
    if private and row.state == 'pending' and configured:
        d = unseal(row)
        if (valid_link(row.provider, d.get('url')) and row.expires_at and row.expires_at > now()
                and d.get('client_fingerprint') == hashlib.sha256(('\0'.join(credentials(row.provider))).encode()).hexdigest()):
            output.update(authorization_url=d['url'], user_code=d['user_code'])
    return output


def legacy_operate(db, actor, provider, action='status', private=False):
    row = locked(db, actor.id, provider)
    previous = row.state
    if action in ('cancel', 'disconnect'):
        row.state, row.encrypted, row.expires_at, row.next_poll_at = 'disconnected', '', None, None
    elif action == 'start' and row.state not in ('pending', 'connected'):
        if not all(credentials(provider)):
            row.state = 'setup_required'
        else:
            try:
                device = begin(provider)
                row.encrypted, row.state = seal(device), 'pending'
                row.expires_at = now() + timedelta(seconds=device['expires_in'])
                row.next_poll_at = now() + timedelta(seconds=device['interval'])
            except Exception:
                row.state, row.encrypted = 'setup_required', ''
    elif action == 'refresh' and row.state == 'connected':
        try:
            token = unseal(row)['access_token']
            if provider == 'feishu':
                result = request('GET', 'https://open.feishu.cn/open-apis/authen/v1/user_info', headers={'Authorization': 'Bearer ' + token})
                valid = result.get('code') == 0 and bool(result.get('data', {}).get('open_id'))
            else:
                result = request('GET', 'https://mcp.dingtalk.com/cli/cliAuthEnabled', headers={'x-user-access-token': token})
                valid = result.get('success') is True and (result.get('result') or {}).get('cliAuthEnabled') is True
            if not valid:
                row.state, row.encrypted = 'expired', ''
        except PermissionError:
            row.state, row.encrypted = 'expired', ''
        except Exception:
            row.state, row.encrypted = 'expired', ''
    elif action == 'refresh' and row.state == 'pending' and (not row.next_poll_at or row.next_poll_at <= now()):
        try:
            d = unseal(row)
            state, tokens = poll(provider, d)
            if state in ('authorization_pending', 'slow_down'):
                if state == 'slow_down':
                    d['interval'] = min(d['interval'] + 5, 60)
                    row.encrypted = seal(d)
                row.next_poll_at = now() + timedelta(seconds=d['interval'])
            else:
                row.state = state
                row.encrypted = seal(tokens) if state == 'connected' else ''
                row.expires_at = now() + timedelta(seconds=max(1, min(int(tokens.get('expires_in', 7200)), 7200))) if state == 'connected' else None
        except PermissionError:
            row.state, row.encrypted = 'expired', ''
        except Exception:
            # Bounded retry on next explicit refresh; original expiry remains authoritative.
            row.next_poll_at = now() + timedelta(seconds=15)
    row.updated_at = now()
    if row.state != previous or action in ('cancel', 'disconnect'):
        from .service import audit
        audit(db, actor, 'platform.' + action, provider, {'state': row.state})
    return view(row, private)


def operate(db, actor, provider, action='status', private=False):
    from .platform_broker import operate as broker_operate
    return broker_operate(db, actor, provider, action, private)
