"""把部署配置里的模型 API Key 换成新的；新 Key 不会出现在命令行、日志或聊天里。

    python3 scripts/rotate_model_key.py                          轮换 .env.docker 里的 OPENAI_API_KEY
    python3 scripts/rotate_model_key.py .env.<名称>
    python3 scripts/rotate_model_key.py --key-file 文件          新 Key 从私有文件读取，成功后删除该文件

先在模型服务商的控制台生成新 Key，再运行本脚本，在隐藏提示里粘贴；或者让别人（如协助的 Agent）代为执行：
先 `umask 077; pbpaste > 文件`（Key 直接从剪贴板进文件，不经过聊天或命令行参数），再用 --key-file 指定它。
密钥文件必须是属于当前用户、不可被组或其他人访问的普通文件（不能是符号链接），且不超过 512 字节。脚本依次：
1. 用新 Key 请求配置的端点 /models，确认可用（配置的模型在列表里）后才写文件；
2. 原子地改写配置文件（权限 0600，其余内容原样保留）；
3. 再用旧 Key 请求一次，确认服务商已经拒绝它。
只输出 HTTP 状态码，不输出任何 Key。写完后需要重建 runner 才会生效；旧 Key 必须在服务商控制台吊销。
"""
from pathlib import Path
import getpass
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r'\.env(\.[A-Za-z0-9_-]{1,40})?')
KEY_NAME = 'OPENAI_API_KEY'
KEY_FORMAT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{15,199}')
DEFAULT_ENDPOINT = 'https://api.openai.com/v1'
REFUSED = (401, 403)
KEY_FILE_LIMIT = 512
USAGE = '用法：rotate_model_key.py [.env 或 .env.<名称>] [--key-file 文件]'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # The key may only reach the configured address.


def probe(endpoint, key, model=None, proxy=None):
    """(HTTP status, whether the model is listed) of GET <endpoint>/models; (None, None) if unreachable.

    Like the runner, it connects directly unless the deployment names a proxy: ambient proxy settings of this
    machine are ignored, so a pass here means the runner can reach the endpoint the same way.
    """
    request = urllib.request.Request(endpoint.rstrip('/') + '/models',
                                     headers={'Authorization': f'Bearer {key}', 'Accept': 'application/json'})
    handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {})
    try:
        with urllib.request.build_opener(handler, NoRedirect).open(request, timeout=15) as response:
            status, body = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None
    try:
        ids = [item['id'] for item in json.loads(body)['data']]
    except (ValueError, KeyError, TypeError):
        return status, None
    return status, (model in ids if model else True)


def read_values(text):
    values = {}
    for line in text.splitlines():
        if line and not line.startswith('#') and '=' in line:
            name, value = line.split('=', 1)
            values[name] = value
    return values


def endpoint_of(values):
    endpoint = values.get('CODEX_BASE_URL', '').strip() or DEFAULT_ENDPOINT
    parts = urlsplit(endpoint)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise SystemExit('CODEX_BASE_URL 必须是不带账号和查询串的 https 地址；Key 只会发往这个地址。')
    return endpoint


def proxy_of(values):
    proxy = values.get('CODEX_PROXY_URL', '').strip()
    if proxy and urlsplit(proxy).scheme not in ('http', 'https'):
        raise SystemExit('CODEX_PROXY_URL 是 socks 代理，本脚本无法替 runner 验证；请在服务商控制台或 runner 里手动验证。')
    return proxy or None


def write_private(path, text):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.env-rotate-')
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_key_file(path):
    """The new key from a private file the operator created, so it never passes through chat or an argument."""
    try:
        # O_NOFOLLOW refuses a symlink; O_NONBLOCK keeps a FIFO from hanging the open (it is rejected below).
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise SystemExit('密钥文件不存在、是符号链接或无法读取。') from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit('密钥文件必须是普通文件。')
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SystemExit('密钥文件必须属于当前用户，且不能被组或其他人访问（chmod 600）。')
        data = os.read(descriptor, KEY_FILE_LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(data) > KEY_FILE_LIMIT:
        raise SystemExit('密钥文件过大，应只包含一个 Key。')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        raise SystemExit('密钥文件不是文本。') from None


def parse(args):
    name, key_file, rest = None, None, list(args)
    while rest:
        item = rest.pop(0)
        if item == '--key-file' and key_file is None and rest:
            key_file = rest.pop(0)
        elif name is None and not item.startswith('-'):
            name = item
        else:
            raise SystemExit(USAGE)
    return name or '.env.docker', key_file


def main(argv=None, root=ROOT, ask=getpass.getpass, check=probe, out=print):
    name, key_file = parse(sys.argv[1:] if argv is None else argv)
    if not NAME.fullmatch(name) or name == '.env.example':
        raise SystemExit(USAGE)
    path = root / name
    if not path.is_file():
        raise SystemExit(f'{name} 不存在。')
    text = path.read_text()
    lines = text.splitlines()
    positions = [i for i, line in enumerate(lines) if line.startswith(KEY_NAME + '=')]
    if len(positions) != 1:
        raise SystemExit(f'{name} 里必须恰好有一行 {KEY_NAME}=。')
    values = read_values(text)
    old, model, endpoint = values[KEY_NAME], values.get('CODEX_MODEL', '').strip() or None, endpoint_of(values)
    proxy = proxy_of(values)
    new = (read_key_file(key_file) if key_file is not None else ask('新的 API Key（输入不会显示）：')).strip()
    if not KEY_FORMAT.fullmatch(new):
        out('新 Key 的格式不对（只允许字母、数字和 . _ -，16–200 位，不含空格或引号）。未修改任何文件。')
        return 2
    if new == old:
        out('新 Key 与现有的相同，这不是轮换。未修改任何文件。')
        return 2
    status, listed = check(endpoint, new, model, proxy)
    if status != 200 or listed is False:
        reason = '服务商拒绝了新 Key' if status in REFUSED else '端点上没有配置的模型' if status == 200 else '无法连接端点或端点异常'
        out(f'新 Key 验证未通过（HTTP {status}）：{reason}。未修改任何文件。')
        return 2
    lines[positions[0]] = f'{KEY_NAME}={new}'
    write_private(path, '\n'.join(lines) + '\n')
    out(f'新 Key 验证通过（HTTP 200），已写入 {name}（权限 0600）。')
    if key_file is not None:
        # The key now lives in the configuration file; do not leave a second plaintext copy behind.
        try:
            os.unlink(key_file)
            out('已删除密钥文件。')
        except OSError:
            out('警告：无法删除密钥文件，请手动删除它。')
    status, _ = check(endpoint, old, model, proxy)
    if status in REFUSED:
        out(f'旧 Key 已被服务商拒绝（HTTP {status}），吊销已生效。')
        code = 0
    elif status == 200:
        out('警告：旧 Key 仍然有效（HTTP 200）。请立刻到服务商控制台吊销它。')
        code = 3
    else:
        out(f'无法确认旧 Key 是否已失效（HTTP {status}）。请到服务商控制台确认它已被吊销。')
        code = 3
    out('下一步：docker compose --env-file .env.docker up -d --no-deps runner')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
