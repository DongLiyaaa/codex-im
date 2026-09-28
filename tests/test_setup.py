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
