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
SCOPES = {'feishu': 'docx:document:readonly', 'dingtalk': 'openid corpid'}
HOSTS = {'feishu': {'accounts.feishu.cn', 'open.feishu.cn'},
         'dingtalk': {'login.dingtalk.com', 'open-dev.dingtalk.com'}}
SETUP = '请管理员配置独立的 PLATFORM_{PROVIDER}_CLIENT_ID / CLIENT_SECRET，确认用户 OAuth 与设备授权已开通。机器人密钥不会自动复用。钉钉还需组织管理员开通本人 CLI 数据访问。'


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
        if response.status == 401:
            raise PermissionError('authorization_expired')
        if response.status >= 500 or 300 <= response.status < 400:
            raise ValueError('upstream_unavailable')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('invalid_response')
        return result
    finally:
        connection.close()



def begin(provider):
    client_id, secret = credentials(provider)
    if provider == 'feishu':
        data = request('POST', 'https://accounts.feishu.cn/oauth/v1/device_authorization',
                       data={'client_id': client_id, 'scope': SCOPES[provider]}, auth=(client_id, secret))
        result = {k: data.get(k) for k in ('device_code', 'user_code', 'expires_in', 'interval')}
        result['url'] = data.get('verification_uri_complete') or data.get('verification_uri')
    else:
        data = request('POST', 'https://login.dingtalk.com/oauth2/device/code.json',
                       data={'client_id': client_id, 'scope': SCOPES[provider]})
        if data.get('success') is not True:
            raise ValueError('device_setup_required')
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
        if not data.get('access_token'):
            return 'expired', {}
        granted = set(data.get('scope', '').split())
        expected = set(SCOPES[provider].split())
        if not expected.issubset(granted) or not granted.issubset(expected | {'offline_access'}):
            return 'setup_required', {}
        return 'connected', {'access_token': data['access_token'], 'expires_in': data.get('expires_in', 7200)}
    if device.get('flow_id'):
        data = request('GET', 'https://mcp.dingtalk.com/cli/oauth/device/poll', params={'flowId': device['flow_id']})
        result = data.get('data') or data.get('result') or {}
        status = result.get('status')
        if status == 'PENDING':
            return 'authorization_pending', {}
        if status != 'APPROVED':
            return 'expired', {}
        code = result.get('authCode')
    else:
        data = request('POST', 'https://login.dingtalk.com/oauth2/device/token.json', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device['device_code'], 'client_id': client_id})
        result = data.get('result') or {}
        if result.get('error') in ('authorization_pending', 'slow_down'):
            return result['error'], {}
        code = result.get('authCode')
    if not code:
        return 'expired', {}
    data = request('POST', 'https://api.dingtalk.com/v1.0/oauth2/userAccessToken', json={
        'clientId': client_id, 'clientSecret': secret, 'code': code, 'grantType': 'authorization_code'})
    token = data.get('accessToken')
    if not token:
        return 'setup_required', {}
    allowed = request('GET', 'https://mcp.dingtalk.com/cli/cliAuthEnabled', headers={'x-user-access-token': token})
    if allowed.get('success') is not True or (allowed.get('result') or {}).get('cliAuthEnabled') is not True:
        return 'setup_required', {}
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
    output = {'provider': row.provider, 'state': row.state if configured else 'setup_required',
              'scope': SCOPES[row.provider], 'expires_at': row.expires_at.isoformat() if row.expires_at else None,
              'message': SETUP.replace('{PROVIDER}', row.provider.upper()) if not configured or row.state == 'setup_required'
              else {'starting': '后台正在发起官方设备授权。', 'platform_rejected': '平台设备授权被拒绝或暂不可用，请管理员核对官方开通状态。',
                    'identity_mismatch': '授权平台身份与当前绑定的本人身份不符，凭据已丢弃。',
                    'interrupted': '授权过程已中断。为避免重复交换或重复私发，请重新发起。'}.get(row.state,
                    '后台确认授权状态；完成后请重新发送任务，不会自动执行此前任务。')}
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
