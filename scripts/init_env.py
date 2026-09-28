"""生成权限为0600的独立部署配置，存在时拒绝覆盖。"""
from pathlib import Path
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / '.env'
text = (ROOT / '.env.example').read_text()
for key in ('POSTGRES_PASSWORD', 'SESSION_SECRET', 'RUNNER_TOKEN', 'BOOTSTRAP_ADMIN_PASSWORD'):
    lines = text.splitlines()
    text = '\n'.join(f'{key}={secrets.token_hex(24)}' if line.startswith(key + '=') else line for line in lines) + '\n'
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('.env 已存在，保留现有配置。')
with os.fdopen(fd, 'w') as out:
    out.write(text)
print(f'已创建 {target}；初始管理员凭据保存在其中，未输出密钥。')
