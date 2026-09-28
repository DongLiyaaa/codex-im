"""Allowlisted HTTPS downloads with DNS validation and connection pinned to validated IP."""
import http.client
import ipaddress
import json
import os
import socket
import ssl
from urllib.parse import urlsplit, urljoin, quote
from . import attachment_storage as storage, attachments, im, im_settings
from .attachment_models import Attachment


class DownloadError(RuntimeError):
    retryable = True


class HTTPDownloadError(DownloadError):
    def __init__(self, status, code=None):
        self.status, self.code = status, code
        self.retryable = status == 429 or status >= 500
        super().__init__('ATTACHMENT_HTTP_' + str(status) + ('_CODE_' + str(code) if code is not None else ''))


def public_addresses(addresses):
    return bool(addresses) and all(ipaddress.ip_address(a).is_global and not
        getattr(ipaddress.ip_address(a), 'ipv4_mapped', None) for a in addresses)


def resolve_addresses(host):
    addresses = list(dict.fromkeys(entry[4][0] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    # Fake-IP DNS cannot be connected to safely. Resolve ONLY the fixed Feishu API
    # through a fixed, TLS-authenticated public resolver; never connect to Fake-IP.
    benchmark = ipaddress.ip_network('198.18.0.0/15')
    if host == 'open.feishu.cn' and addresses and all(ipaddress.ip_address(a) in benchmark for a in addresses):
        connection = PinnedHTTPS('cloudflare-dns.com', '1.1.1.1')
        try:
            connection.request('GET', '/dns-query?name=open.feishu.cn&type=A', headers={'Accept': 'application/dns-json'})
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise DownloadError('ATTACHMENT_DNS_UNAVAILABLE')
            data = json.loads(raw)
            if data.get('Status') != 0:
                raise DownloadError('ATTACHMENT_DNS_UNAVAILABLE')
            addresses = [answer['data'] for answer in data.get('Answer', []) if answer.get('type') == 1]
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
            raise DownloadError('ATTACHMENT_DNS_UNAVAILABLE') from None
        finally:
            connection.close()
    if not public_addresses(addresses):
        raise RuntimeError('附件下载地址解析到受限网络')
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def request(url, *, method='GET', headers=None, body=None, allowed=(), max_bytes=20 * 1024 * 1024):
    import time
    deadline = time.monotonic() + 45
    for hop in range(4):
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or not any(host == domain or host.endswith('.' + domain) for domain in allowed)):
            raise RuntimeError('附件下载地址不在允许的HTTPS域名范围内')
        addresses = resolve_addresses(host)
        connection = PinnedHTTPS(host, addresses[0])
        try:
            connection.request(method, parsed.path + ('?' + parsed.query if parsed.query else ''), body=body, headers=headers or {})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                next_url = urljoin(url, response.getheader('location', ''))
                # Never forward tokens to another host, even if allowlisted.
                if headers and urlsplit(next_url).hostname != host:
                    raise RuntimeError('携带凭据的附件请求禁止跨域跳转')
                if method != 'GET':
                    raise RuntimeError('附件下载接口不允许重定向')
                url = next_url
                continue
            if response.status != 200:
                code = None
                try:
                    error_body = response.read(65537)
                    if len(error_body) <= 65536:
                        value = json.loads(error_body).get('code')
                        if type(value) is int:
                            code = value
                except (ValueError, AttributeError):
                    pass
                raise HTTPDownloadError(response.status, code)
            length = response.getheader('content-length')
            if length and int(length) > max_bytes:
                raise RuntimeError('附件超过下载大小限制')
            result = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise DownloadError('附件下载超时')
                chunk = response.read(min(65536, max_bytes + 1 - len(result)))
                if not chunk:
                    return bytes(result)
                result.extend(chunk)
                if len(result) > max_bytes:
                    raise RuntimeError('附件超过下载大小限制')
        except (OSError, http.client.HTTPException):
            raise DownloadError('附件下载网络暂不可用') from None
        finally:
            connection.close()
    raise RuntimeError('附件下载重定向次数过多')


def download(identifier, factory):
    import httpx
    with factory.begin() as db:
        item = db.get(Attachment, identifier)
        attachments.authorize(db, item)
        reference = json.loads(im_settings.cipher().decrypt(item.encrypted_reference.encode()))
        with im_settings.snapshot(db, item.provider):
            from .im_discovery import scope
            if item.app_scope != scope(item.provider):
                raise RuntimeError('应用配置已变更，附件下载已停止')
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                token = im.access_token(client, item.provider)
            if item.provider == 'feishu':
                url = 'https://open.feishu.cn/open-apis/im/v1/messages/' + quote(reference['message_id'], safe='') + '/resources/' + quote(reference['key'], safe='') + '?type=' + reference['type']
                raw = request(url, headers={'Authorization': 'Bearer ' + token}, allowed=('open.feishu.cn',), max_bytes=storage.limit('MAX_BYTES', 20 * 1024 * 1024))
            else:
                response = request('https://api.dingtalk.com/v1.0/robot/messageFiles/download', method='POST',
                    headers={'x-acs-dingtalk-access-token': token, 'Content-Type': 'application/json'},
                    body=json.dumps({'downloadCode': reference['downloadCode'], 'robotCode': im._required('DINGTALK_ROBOT_CODE')}).encode(), allowed=('api.dingtalk.com',), max_bytes=65536)
                url = json.loads(response).get('downloadUrl')
                if not isinstance(url, str) or len(url) > 8192:
                    raise RuntimeError('平台没有返回有效的附件下载地址')
                domains = tuple(os.getenv('ATTACHMENT_DINGTALK_DOWNLOAD_DOMAINS', 'dingtalk.com,dingtalkapps.com,alicdn.com,aliyuncs.com').split(','))
                raw = request(url, allowed=domains, max_bytes=storage.limit('MAX_BYTES', 20 * 1024 * 1024))
        db.refresh(item)
        attachments.authorize(db, item)
        target = storage.path(item.id)
        target.unlink(missing_ok=True)
        item.size, item.checksum = storage.save_stream(item.id, [raw])
