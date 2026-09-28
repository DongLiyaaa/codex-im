from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / '.env').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        os.environ.setdefault(key, value)
sys.path.insert(0, str(ROOT / 'runner'))
local_cli = ROOT / '.runtime/codex-cli/node_modules/.bin'
if (local_cli / 'codex').is_file():
    os.environ['PATH'] = str(local_cli) + os.pathsep + os.environ.get('PATH', '')
# Local default is this project's explicit login source; API mode can override it.
os.environ.setdefault('CODEX_AUTH_MODE', 'chatgpt')
os.environ.setdefault('CODEX_OAUTH_AUTH_FILE', str(ROOT / '.runtime/codex-oauth/auth.json'))
import uvicorn
uvicorn.run('main:app', host='127.0.0.1', port=18202, access_log=False)
