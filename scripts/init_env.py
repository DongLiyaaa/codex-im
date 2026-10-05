"""生成权限为0600的独立部署配置，存在时拒绝覆盖。

    python scripts/init_env.py                     本机直接运行：创建 .env
    python scripts/init_env.py .env.docker 18210   Docker Compose：创建 .env.docker，网页只监听本机 18210
"""
from pathlib import Path
import os
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
RANDOM_KEYS = ('POSTGRES_PASSWORD', 'SESSION_SECRET', 'RUNNER_TOKEN', 'PLATFORM_BRIDGE_KEY')
PORT_LINES = ('APP_ORIGIN', 'PLATFORM_BRIDGE_URL', 'ATTACHMENT_BRIDGE_URL')
NAME = re.compile(r'\.env(\.[A-Za-z0-9_-]{1,40})?')


def render(example, port=None):
    lines = []
    for line in example.splitlines():
        key = line.split('=', 1)[0]
        if key in RANDOM_KEYS and '=' in line:
            line = f'{key}={secrets.token_hex(24)}'
        elif port and key == 'HUB_PORT':
            line = f'HUB_PORT={port}'
        elif port and key in PORT_LINES:
            line = line.replace(':18200', f':{port}')
        lines.append(line)
    return '\n'.join(lines) + '\n'


def main(argv=None, root=ROOT):
    args = list(sys.argv[1:] if argv is None else argv)
    name = args[0] if args else '.env'
    if len(args) > 2 or not NAME.fullmatch(name) or name == '.env.example':
        raise SystemExit('用法：init_env.py [.env 或 .env.<名称>] [端口]')
    port = None
    if len(args) == 2:
        if not args[1].isdigit() or not 1024 <= int(args[1]) <= 65535:
            raise SystemExit('端口必须是 1024–65535 的整数。')
        port = int(args[1])
    try:
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(f'{name} 已存在，保留现有配置。')
    with os.fdopen(fd, 'w') as out:
        out.write(render((root / '.env.example').read_text(), port))
    print(f'已创建 {name}；未输出密钥。启动后在网页完成首次管理员注册。')


if __name__ == '__main__':
    main()
