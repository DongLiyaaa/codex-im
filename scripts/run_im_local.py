"""Local IM process; dedicated project PostgreSQL, no Docker or runner changes."""
from pathlib import Path
import os
import sys
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
for key, value in dotenv_values(ROOT / '.env').items():
    if value is not None and (key.startswith(('FEISHU_', 'DINGTALK_')) or key in ('DATABASE_URL', 'SESSION_SECRET', 'IM_CONFIG_KEY')):
        os.environ.setdefault(key, value)
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg:///agent_hub?host=' + str(ROOT / '.runtime/pgsocket') + '&port=55439')
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['PYTHONPATH'] = str(ROOT / 'backend') + os.pathsep + os.environ.get('PYTHONPATH', '')
from app.im_connections import main
main()
