"""独立本地附件worker；不启动容器，不改写OAuth。"""
from pathlib import Path
import os
import sys
ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / '.env').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        os.environ.setdefault(key, value)
os.environ['DATABASE_URL'] = 'postgresql+psycopg:///agent_hub?host=' + str(ROOT / '.runtime/pgsocket') + '&port=55439'
sys.path.insert(0, str(ROOT / 'backend'))
from app.attachment_worker import main
main()
