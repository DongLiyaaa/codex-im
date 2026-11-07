"""First administrator tests use only the existing isolated PostgreSQL fixture."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, func
from test_im_postgres import database
from app import main, schemas, setup, security
from app.models import User, Identity, SetupState


BODY = {'email': 'Owner@example.invalid', 'name': '<owner>', 'password': 'test-password-only-123'}


def empty(database):
    with database.begin() as db:
        db.execute(delete(Identity))
        db.execute(delete(User))


def http(database):
    def get_db():
        with database() as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
    main.app.dependency_overrides[main.get_db] = get_db
    security._attempts.clear()
    return TestClient(main.app)


def test_first_registration_concurrency_and_tombstone(database):
    empty(database)
    barrier = Barrier(2)
    def register(index):
        barrier.wait()
        try:
            with database.begin() as db:
                setup.create_admin(db, schemas.SetupAdmin(**(BODY | {'email': f'owner{index}@example.invalid'})))
            return 201
        except HTTPException as e:
            return e.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register, [1, 2])) == [201, 409]
    with database.begin() as db:
        user = db.scalar(select(User))
        assert user.role == 'super_admin' and user.active and user.org_id is None
        assert user.onboarding == {'status': 'pending', 'step': 'welcome', 'auto_start': True}
        assert security.verify_password(BODY['password'], user.password_hash)
        assert db.scalar(select(func.count()).select_from(User)) == 1
        db.execute(delete(User))
    with database.begin() as db:
        assert setup.initialized(db)
        with pytest.raises(HTTPException):
            setup.create_admin(db, schemas.SetupAdmin(**BODY))


def test_existing_inactive_user_blocks_and_marks(database):
    with database.begin() as db:
        db.scalar(select(User)).active = False
        assert setup.initialized(db)
    with database.begin() as db:
        assert db.get(SetupState, 'initial-admin').initialized
        with pytest.raises(HTTPException): setup.create_admin(db, schemas.SetupAdmin(**BODY))


def test_rollback_can_retry(database):
    empty(database)
    with pytest.raises(RuntimeError):
        with database.begin() as db:
            setup.create_admin(db, schemas.SetupAdmin(**BODY))
            raise RuntimeError('rollback')
    with database.begin() as db:
        assert not setup.initialized(db)
        assert setup.create_admin(db, schemas.SetupAdmin(**BODY)).email == 'owner@example.invalid'


def test_http_validation_origin_login_and_rate_limit(database, monkeypatch):
    empty(database)
    monkeypatch.setenv('APP_ORIGIN', 'http://127.0.0.1:18200')
    c = http(database)
    try:
        assert c.get('/api/setup/status').json() is False
        for extra in [{'role': 'super_admin'}, {'active': True}, {'org_id': 'other'}, {'password': 'short'}, {'email': '<bad>@example.invalid'}]:
            r = c.post('/api/setup/bootstrap', json=BODY | extra)
            assert r.status_code == 422
            assert BODY['password'] not in r.text and 'short' not in r.text
        assert c.post('/api/setup/bootstrap', json=BODY, headers={'Origin': 'https://foreign.invalid'}).status_code == 403
        r = c.post('/api/setup/bootstrap', json=BODY)
        assert r.status_code == 201 and r.json() == {'ok': True}
        assert 'set-cookie' not in r.headers
        assert c.get('/api/setup/status').json() is True
        assert c.post('/api/setup/bootstrap', json=BODY).status_code == 409
        assert c.post('/api/auth/login', json={'email': BODY['email'], 'password': BODY['password']}).status_code == 200
        for _ in range(10): r = c.post('/api/setup/bootstrap', json=BODY)
        assert r.status_code == 429
    finally:
        c.close()
        main.app.dependency_overrides.clear()


def test_existing_instance_ignores_bootstrap_and_empty_env_starts(database, monkeypatch):
    monkeypatch.setattr(main, 'SessionLocal', database)
    monkeypatch.setattr(main.service.Worker, 'start', lambda self: None)
    monkeypatch.setattr(main.service.Worker, 'stop', lambda self: None)
    monkeypatch.setattr(main.Base.metadata, 'create_all', lambda *a, **k: None)
    import app.im_migrations
    monkeypatch.setattr(app.im_migrations, 'migrate', lambda *a: None)
    monkeypatch.setenv('BOOTSTRAP_ADMIN_EMAIL', 'invalid')
    monkeypatch.setenv('BOOTSTRAP_ADMIN_PASSWORD', 'invalid')
    with TestClient(main.app): pass
    empty(database)
    # The durable marker created above keeps setup closed even with no users.
    with TestClient(main.app): pass
    with database.begin() as db: db.execute(delete(SetupState))
    monkeypatch.delenv('BOOTSTRAP_ADMIN_EMAIL')
    monkeypatch.delenv('BOOTSTRAP_ADMIN_PASSWORD')
    with TestClient(main.app): pass
    with database.begin() as db: assert not setup.initialized(db)
    monkeypatch.setenv('BOOTSTRAP_ADMIN_EMAIL', BODY['email'])
    monkeypatch.setenv('BOOTSTRAP_ADMIN_PASSWORD', BODY['password'])
    with TestClient(main.app): pass
    with database.begin() as db:
        assert setup.initialized(db)
        assert db.scalar(select(User)).email == 'owner@example.invalid'
        assert db.scalar(select(User)).onboarding == {'status': 'pending', 'step': 'welcome', 'auto_start': True}


def test_onboarding_migration_is_additive_and_repeatable(database):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app.im_migrations import migrate
    engine = database.kw['bind']
    schema = engine.get_execution_options()['schema_translate_map'][None]
    with engine.begin() as conn:
        quoted = engine.dialect.identifier_preparer.quote(schema)
        conn.exec_driver_sql('SET LOCAL search_path TO ' + quoted)
        before = conn.exec_driver_sql('SELECT id, name FROM users ORDER BY id').all()
        conn.exec_driver_sql('ALTER TABLE users DROP COLUMN onboarding')
        transaction = SimpleNamespace(begin=lambda: nullcontext(conn))
        migrate(transaction)
        migrate(transaction)
        assert conn.exec_driver_sql('SELECT id, name FROM users ORDER BY id').all() == before
        assert conn.exec_driver_sql('SELECT onboarding FROM users').scalars().all() == [None] * len(before)
        rows = conn.exec_driver_sql("SELECT is_nullable, column_default FROM information_schema.columns WHERE table_schema = %s AND table_name = 'users' AND column_name = 'onboarding'", (schema,)).all()
        assert rows == [('YES', None)]


def test_onboarding_survives_login_and_does_not_change_other_users(database):
    empty(database)
    client = http(database)
    try:
        assert client.post('/api/setup/bootstrap', json=BODY).status_code == 201
        login = {'email': BODY['email'], 'password': BODY['password']}
        assert client.post('/api/auth/login', json=login).json()['onboarding']['auto_start'] is True
        with database.begin() as db:
            other = User(email='other@example.invalid', name='Other', role='super_admin', active=True,
                         password_hash=security.hash_password(BODY['password']))
            db.add(other); db.flush()
            other_id = other.id
        assert client.put('/api/auth/me/onboarding', json={'status': 'in_progress', 'step': 'people'}).status_code == 200
        assert client.post('/api/auth/logout').status_code == 200
        resumed = client.post('/api/auth/login', json=login).json()
        assert resumed['onboarding'] == {'status': 'in_progress', 'step': 'people', 'auto_start': True}
        assert client.put('/api/auth/me/onboarding', json={'status': 'completed', 'step': 'welcome'}).status_code == 422
        assert client.put('/api/auth/me/onboarding', json={'user_id': other_id, 'status': 'deferred', 'step': 'people'}).status_code == 422
        deferred = client.put('/api/auth/me/onboarding', json={'status': 'deferred', 'step': 'people'}).json()
        assert deferred['onboarding']['auto_start'] is False
        with database() as db:
            assert db.get(User, other_id).onboarding is None
        assert client.put('/api/auth/me/onboarding', json={'status': 'completed', 'step': 'try'}).status_code == 200
        assert client.put('/api/auth/me/onboarding', json={'status': 'in_progress', 'step': 'welcome'}).json()['onboarding']['auto_start'] is False
    finally:
        client.close()
        main.app.dependency_overrides.clear()
