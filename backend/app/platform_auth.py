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
    'user_not_allowed': ('Agent Hub 管理员未把你列入本平台「个人授权可用人员」，暂不能以你本人身份使用云文档等能力。请联系 Agent Hub 超级管理员在「IM 集成 → 个人授权可用人员」中添加你。', 'contact_hub_admin'),
    'identity_missing': ('缺少目标平台当前应用下的本人身份绑定，请管理员明确绑定后重试；不能借用消息来源平台身份。', 'bind_target_identity'),
    'identity_app_mismatch': ('个人 OAuth 与机器人不是同一应用，无法安全核验应用范围内的本人身份；请管理员核对配置。', 'configure_application'),
    'private_delivery_unsupported': ('钉钉旧 webhook 不支持私发本人授权材料，请管理员配置 Stream 应用后重试。', 'configure_private_delivery'),
    'private_delivery_failed': ('授权材料私发失败，本次材料已清除；请检查机器人私发权限及本人绑定后重新发起。', 'check_private_delivery'),
    'starting': ('后台正在发起官方设备授权，仅向官方服务出站 HTTPS。', 'wait'),
    'pending': ('等待本人完成官方授权，后台轮询确认；完成后请重新发送任务。', 'approve_personally'),
    'connected': ('本人授权已连接，可以以你本人身份创建、读取和编辑云文档/表格/多维表格；请重新发送任务。', 'resend_task'),
    'identity_mismatch': ('授权平台身份与当前绑定的本人身份不符，凭据已丢弃。', 'verify_target_identity'),
    'identity_unverified': ('平台授权已完成，但无法核验授权账号就是绑定的本人，凭据已丢弃。请先在私聊里直接给机器人发一条消息，再重新发起授权；仍失败请联系管理员。', 'retry_authorization'),
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


ENDPOINT_HOSTS = frozenset(('accounts.feishu.cn', 'open.feishu.cn', 'login.dingtalk.com', 'api.dingtalk.com', 'mcp.dingtalk.com',
                            'mcp-gw.dingtalk.com', 'oapi.dingtalk.com'))


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
        # DingTalk only accepts its own CLI client for device authorization (a self-built app is answered with
        # 900103 "应用不存在"); the one-time code is exchanged through DingTalk's MCP proxy, so no app secret is used.
        official = request('GET', 'https://mcp.dingtalk.com/cli/clientId')
        check_response(official)
        official_id = official.get('result')
        if not isinstance(official_id, str) or not 1 <= len(official_id) <= 128 or not official_id.isalnum():
            raise ProviderError('platform_rejected')
        data = request('POST', 'https://login.dingtalk.com/oauth2/device/code.json',
                       data={'client_id': official_id, 'scope': SCOPES[provider]})
        check_response(data)
        if data.get('success') is not True:
            raise ProviderError('platform_rejected')
        d = data['result']
        result = {'device_code': d.get('deviceCode'), 'user_code': d.get('userCode'),
                  'url': d.get('verificationUriComplete') or d.get('verificationUri'),
                  'expires_in': d.get('expiresIn'), 'interval': d.get('interval'), 'flow_id': d.get('flowId'),
                  'client_id': official_id}
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
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device['device_code'], 'client_id': device.get('client_id') or client_id})
        result = data.get('result') or {}
        if result.get('error') in ('authorization_pending', 'slow_down'):
            return result['error'], {}
        check_response(data)
        if result.get('error'):
            raise ProviderError(provider_error(result))
        code = result.get('authCode')
    if not code:
        raise ProviderError('platform_rejected')
    if device.get('client_id'):
        data = request('POST', 'https://mcp.dingtalk.com/oauth2/getToken', json={
            'clientId': device['client_id'], 'authCode': code, 'grantType': 'authorization_code'})
        if data.get('errorCode') or data.get('errorMsg'):
            raise ProviderError(provider_error(data))
    else:
        data = request('POST', 'https://api.dingtalk.com/v1.0/oauth2/userAccessToken', json={
            'clientId': client_id, 'clientSecret': secret, 'code': code, 'grantType': 'authorization_code'})
        check_response(data)
    token = data.get('accessToken')
    if not token:
        raise ProviderError(provider_error(data))
    allowed = request('GET', 'https://mcp.dingtalk.com/cli/cliAuthEnabled', headers={'x-user-access-token': token})
    denial = cli_denial(allowed)
    if denial:
        raise ProviderError('organization_denied', denial)
    connected = {'access_token': token, 'expires_in': data.get('expiresIn', 7200)}
    # The official exchange names the approving account (dws reads the same fields); the worker checks it is the
    # bound person, so no contact permission or open-API identity call is needed for the CLI client's token.
    for field, key in (('userId', 'user_id'), ('corpId', 'corp_id')):
        value = data.get(field)
        if isinstance(value, str) and 0 < len(value.strip()) <= 200:
            connected[key] = value.strip()
    return 'connected', connected


# The official contact MCP server dws asks "who am I" with; it accepts the CLI client's token, which the open API
# (contact/users/me) does not, and needs no member-read permission on the robot.
DINGTALK_CONTACT = 'https://mcp-gw.dingtalk.com/server/db4b26cb38ea6a8739ad55d1997fa1da608cd36b33a6cf0f77884f70c49382fe'


def dingtalk_accounts(token):
    """(corpId, staff id) of every organization account the CLI token speaks for, read as dws reads its profile."""
    data = request('POST', DINGTALK_CONTACT, headers={'x-user-access-token': token, 'Authorization': 'Bearer ' + token,
        'X-Cli-Source': 'dws-cli', 'Accept': 'application/json'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'get_current_user_profile', 'arguments': {}}})
    result = data.get('result')
    if not isinstance(result, dict):
        raise ProviderError('identity_unverified', 'IDENTITY_LOOKUP_FAILED')
    profile = result.get('structuredContent')
    if not isinstance(profile, dict):
        texts = [c.get('text') for c in result.get('content') or [] if isinstance(c, dict) and isinstance(c.get('text'), str)]
        try:
            profile = json.loads(texts[0]) if texts else None
        except ValueError:
            profile = None
    if not isinstance(profile, dict):
        raise ProviderError('identity_unverified', 'IDENTITY_LOOKUP_FAILED')
    if profile.get('success') is False or result.get('isError') is True:
        code = str(profile.get('code') or profile.get('errorCode') or '').upper()
        if code.startswith('PAT_') and code.endswith('NO_PERMISSION'):
            raise ProviderError('identity_unverified', 'IDENTITY_CONTACT_DENIED')
        if code == 'TOKEN_VERIFIED_FAILED':
            raise ProviderError('identity_unverified', 'IDENTITY_TOKEN_REJECTED')
        raise ProviderError('identity_unverified', 'IDENTITY_LOOKUP_FAILED')
    entries = profile.get('result')
    accounts = []
    for entry in entries if isinstance(entries, list) else [entries]:
        if not isinstance(entry, dict):
            continue
        model = entry.get('orgEmployeeModel') if isinstance(entry.get('orgEmployeeModel'), dict) else entry
        user = model.get('userId') or model.get('userid') or model.get('orgUserId')
        corp = model.get('corpId')
        if isinstance(user, str) and 0 < len(user) <= 200:
            accounts.append((corp if isinstance(corp, str) and 0 < len(corp) <= 200 else None, user))
    return accounts


def cli_denial(status):
    """Why DingTalk refuses CLI data access, classified as the official dws CLI does; None when it is allowed.

    The org-level switch is not implied by being an administrator: someone has to turn on CLI 数据访问 for the
    organization (and, when restricted, include the user) in the developer settings."""
    code = str(status.get('errorCode') or '').upper()
    if code in ('CHANNEL_REQUIRED', 'ENTERPRISE_NOT_AUTHORIZED', 'NO_AUTH'):
        return code
    result = status.get('result')
    if status.get('success') is not True or not isinstance(result, dict):
        return 'CLI_STATUS_UNKNOWN'
    if result.get('cliAuthEnabled') is True:
        return None
    if result.get('userScope') == 'forbidden':
        return 'CLI_USER_FORBIDDEN'
    if result.get('channelScope') == 'specified':
        return 'CHANNEL_REQUIRED'
    if result.get('userScope') == 'specified':
        return 'CLI_USER_NOT_ALLOWED'
    return 'CLI_NOT_ENABLED'


DINGTALK_SETTINGS = 'https://open-dev.dingtalk.com/fe/old#/developerSettings'
# Shown instead of the generic organization_denied text when the platform said why.
DENIALS = {
    'CLI_NOT_ENABLED': f'钉钉组织尚未开启「CLI 数据访问」，组织管理员身份不会自动开启。请组织管理员打开钉钉开发者后台 → 基本信息 → CLI设置（{DINGTALK_SETTINGS}），打开「允许成员通过 CLI 访问个人数据」，并在「使用范围管理 → 可用人员设置」选「全员可用」或把你加入指定人员，然后重新发起授权。',
    'CLI_USER_NOT_ALLOWED': f'钉钉组织的「CLI 数据访问」只允许指定人员，你不在名单中。请组织管理员在钉钉开发者后台 → CLI设置（{DINGTALK_SETTINGS}）→ 使用范围管理 → 可用人员设置，把你加入指定人员或改为「全员可用」，然后重新发起授权。',
    'CLI_USER_FORBIDDEN': f'钉钉组织的「CLI 数据访问」可用人员设置为「全员禁止使用」，管理员本人也会被拒绝。请组织管理员在钉钉开发者后台 → CLI设置（{DINGTALK_SETTINGS}）→ 使用范围管理 → 编辑，改为「全员可用」或「指定人员范围可用」并包含你，然后重新发起授权。',
    'CHANNEL_REQUIRED': f'钉钉组织对「CLI 数据访问」开启了渠道管控，只允许指定渠道。请组织管理员在钉钉开发者后台 → CLI设置（{DINGTALK_SETTINGS}）取消渠道限制，然后重新发起授权。',
    'ENTERPRISE_NOT_AUTHORIZED': '本次请求未通过钉钉企业安全认证，请组织管理员检查企业安全策略后重新发起授权。',
    'NO_AUTH': '钉钉返回授权已失效，请重新发起授权。',
    'CLI_STATUS_UNKNOWN': '钉钉未返回 CLI 数据访问状态，请稍后重新发起授权。',
}


# Shown instead of the generic identity_unverified text when it is known why the account could not be checked.
UNVERIFIED = {
    'IDENTITY_ORG_UNKNOWN': '平台授权已完成，但 Hub 还不知道你所在的钉钉组织，无法核验授权账号是否为本人，凭据已丢弃。请先在钉钉私聊里直接给机器人发一条消息，再重新发起授权。',
    'IDENTITY_CONTACT_DENIED': f'平台授权已完成，但钉钉组织的 CLI 数据访问未开放通讯录「读取当前用户信息」，Hub 无法核验授权账号是否为本人，凭据已丢弃。请组织管理员在钉钉开发者后台 → CLI设置（{DINGTALK_SETTINGS}）开放通讯录个人信息读取，然后重新发起授权。',
    'IDENTITY_TOKEN_REJECTED': '钉钉拒绝了刚签发的授权凭据（可能已过期或被撤销），凭据已丢弃，请重新发起授权。',
    'IDENTITY_LOOKUP_FAILED': '平台授权已完成，但钉钉身份查询暂不可用，无法核验授权账号是否为本人，凭据已丢弃。请稍后重新发起授权；仍失败请联系管理员。',
}


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
        except ProviderError as exc:
            if exc.state != 'organization_denied':
                row.next_poll_at = now() + timedelta(seconds=15)
            else:
                row.state, row.encrypted, row.expires_at = exc.state, '', None
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
