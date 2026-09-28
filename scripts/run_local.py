"""本地验证启动器。使用专属PG Unix socket；不会操作Docker。"""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / '.env').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        os.environ.setdefault(key, value)
os.environ['DATABASE_URL'] = 'postgresql+psycopg:///agent_hub?host=' + str(ROOT / '.runtime/pgsocket') + '&port=55439'
os.environ['RUNNER_URL'] = 'http://127.0.0.1:18202'
os.environ['STATIC_DIR'] = str(ROOT / 'frontend/dist')
os.environ['APP_ORIGIN'] = 'http://127.0.0.1:18200'
sys.path.insert(0, str(ROOT / 'backend'))
import uvicorn
uvicorn.run('app.main:app', host='127.0.0.1', port=18200, access_log=False)
