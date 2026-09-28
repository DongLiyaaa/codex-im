"""真实 PostgreSQL 验证 IM 去重、权限、失败事务回滚，不发外部消息。"""
import os
from pathlib import Path
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('DATABASE_URL', 'postgresql+psycopg:///agent_hub_test?host=' + str(ROOT / '.runtime/pgsocket') + '&port=55439')
os.environ.setdefault('SESSION_SECRET', 'test-secret-for-isolated-tests-only-32-chars')
from app.models import Base, User, Group, Identity, Run, IMEvent
from app.im import _enqueue


@pytest.fixture
def database(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_ID', 'app')
    monkeypatch.setenv('DINGTALK_CLIENT_ID', 'app')
    monkeypatch.setenv('DINGTALK_ROBOT_CODE', 'robot')
    engine = create_engine(os.environ['DATABASE_URL'])
    assert engine.url.database.endswith('_test'), 'Tests require a dedicated *_test database'
    schema = 'imtest_' + uuid.uuid4().hex
    with engine.begin() as c:
        c.exec_driver_sql('CREATE SCHEMA ' + schema)
    isolated = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(isolated)
    factory = sessionmaker(isolated, expire_on_commit=False)
    with factory.begin() as db:
        user = User(email='imtest@example.invalid', name='测试成员', role='member', org_id='org', team_id='team', active=True, password_hash='unused')
        db.add(user)
        db.flush()
        group = Group(name='测试群', org_id='org', team_id='team', member_ids=[user.id], provider='feishu', external_id='chat')
        identity = Identity(provider='feishu', external_user_id='sender', user_id=user.id)
        db.add_all([group, identity])
        db.flush()
        from app.im_discovery import pin, scope
        pin(db, 'identity', identity.id, scope('feishu'))
        pin(db, 'group', group.id, scope('feishu'))
    yield factory
    Base.metadata.drop_all(isolated)
    with engine.begin() as c:
        c.exec_driver_sql('DROP SCHEMA ' + schema)
    engine.dispose()


def test_dedup_and_permissions(database):
    with database.begin() as db:
        assert _enqueue(db, 'feishu', 'event1', 'sender', 'chat', '你好', True) == {'ok': True}
    with database.begin() as db:
        assert _enqueue(db, 'feishu', 'event1', 'sender', 'chat', '你好', True)['duplicate']
        assert db.scalar(select(func.count()).select_from(Run)) == 1
        assert db.scalar(select(func.count()).select_from(IMEvent)) == 1
    for index, (sender, chat) in enumerate([('unknown', 'chat'), ('sender', 'unregistered')]):
        with database.begin() as db:
            assert _enqueue(db, 'feishu', f'denied{index}', sender, chat, '你好', True)['pending']
    with database.begin() as db:
        group = db.scalar(select(Group))
        group.member_ids = []
    with database.begin() as db:
        assert _enqueue(db, 'feishu', 'event3', 'sender', 'chat', '你好', True)['pending']


def test_enqueue_failure_rolls_back_event(database, monkeypatch):
    import app.service as service
    def fail(*args):
        raise RuntimeError('test rollback')
    monkeypatch.setattr(service, 'enqueue_message', fail)
    with pytest.raises(RuntimeError):
        with database.begin() as db:
            _enqueue(db, 'feishu', 'event-rollback', 'sender', 'chat', '你好', True)
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(IMEvent)) == 0
        assert db.scalar(select(func.count()).select_from(Run)) == 0
