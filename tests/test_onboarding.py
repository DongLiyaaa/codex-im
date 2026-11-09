from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main, schemas, setup
from app.models import SetupState, User


@pytest.mark.parametrize('body', [
    {}, {'status': 'pending', 'step': 'welcome'}, {'status': 'in_progress', 'step': 'unknown'},
    {'status': 'completed', 'step': 'welcome'}, {'status': 'in_progress', 'step': None},
    {'status': 'in_progress', 'step': 'welcome', 'auto_start': True},
    {'status': 'deferred', 'step': 'model', 'user_id': 'other'},
    {'status': 'completed', 'step': 'try', 'role': 'super_admin'},
])
def test_onboarding_input_rejects_invalid_or_unowned_fields(body):
    with pytest.raises(ValidationError):
        schemas.OnboardingUpdate(**body)


def test_only_bootstrap_sets_automatic_onboarding(monkeypatch):
    monkeypatch.setattr(setup, 'initialized', lambda db: False)
    monkeypatch.setattr(setup, 'hash_password', lambda password: 'test-hash')
    rows = []
    db = SimpleNamespace(get=lambda *args: None, add=rows.append, flush=lambda: None)
    user = setup.create_admin(db, schemas.SetupAdmin(email='owner@example.invalid', name='Owner', password='test-password-long'))
    assert user.role == 'super_admin'
    assert user.onboarding == {'status': 'pending', 'step': 'model', 'auto_start': True}
    assert any(isinstance(row, SetupState) and row.initialized for row in rows)
    assert User(email='old@example.invalid').onboarding is None
    with pytest.raises(ValidationError):
        schemas.UserCreate(email='member@example.invalid', password='test-password-long', name='Member', role='member',
                           org_id='org', team_id='team', onboarding={'status': 'pending', 'step': 'welcome'})


@pytest.mark.parametrize('initial,status,step,auto', [
    (None, 'in_progress', 'model', False),
    ({'status': 'pending', 'step': 'model', 'auto_start': True}, 'in_progress', 'usage', True),
    ({'status': 'in_progress', 'step': 'usage', 'auto_start': True}, 'deferred', 'usage', False),
    ({'status': 'in_progress', 'step': 'try', 'auto_start': True}, 'completed', 'try', False),
    ({'status': 'completed', 'step': 'try', 'auto_start': False}, 'in_progress', 'model', False),
])
def test_self_progress_and_auto_start_lifecycle(monkeypatch, initial, status, step, auto):
    user = User(id='self', email='self@example.invalid', name='Self', role='super_admin', active=True, onboarding=initial)
    statements, audits = [], []
    def scalar(query):
        statements.append(query)
        return user
    db = SimpleNamespace(scalar=scalar)
    monkeypatch.setattr(main.service, 'audit', lambda *args: audits.append(args))
    response = main.update_onboarding(schemas.OnboardingUpdate(status=status, step=step), user, db)
    assert response.id == 'self' and response.onboarding.auto_start is auto
    assert user.onboarding == {'status': status, 'step': step, 'auto_start': auto}
    assert statements[0].compile().params['id_1'] == user.id
    assert 'FOR UPDATE' in str(statements[0])
    assert audits[0][2:4] == ('user.onboarding_update', 'self')
    assert user.role == 'super_admin' and user.org_id is None
    main.update_onboarding(schemas.OnboardingUpdate(status=status, step=step), user, db)
    assert len(audits) == 1


def test_inactive_or_deleted_actor_cannot_update(monkeypatch):
    monkeypatch.setattr(main.service, 'audit', lambda *args: pytest.fail('audit should not run'))
    for user in (None, User(id='self', active=False)):
        with pytest.raises(HTTPException) as exc:
            main.update_onboarding(schemas.OnboardingUpdate(status='in_progress', step='model'),
                                   SimpleNamespace(id='self'), SimpleNamespace(scalar=lambda _: user))
        assert exc.value.status_code == 401


def test_onboarding_http_requires_session_and_rejects_foreign_user_payload(monkeypatch):
    from app.db import get_db
    user = User(id='self', email='self@example.invalid', name='Self', role='member', active=True)
    client = TestClient(main.app)
    main.app.dependency_overrides[get_db] = lambda: SimpleNamespace(scalar=lambda _: user)
    monkeypatch.setattr(main.service, 'audit', lambda *args: None)
    try:
        assert client.put('/api/auth/me/onboarding', json={'status': 'in_progress', 'step': 'model'}).status_code == 401
        main.app.dependency_overrides[main.current_user] = lambda: user
        bad = client.put('/api/auth/me/onboarding', json={'user_id': 'other', 'status': 'in_progress', 'step': 'model'})
        assert bad.status_code == 422 and user.onboarding is None
        response = client.put('/api/auth/me/onboarding', json={'status': 'deferred', 'step': 'usage'})
        assert response.status_code == 200
        assert response.json()['onboarding'] == {'status': 'deferred', 'step': 'usage', 'auto_start': False}
        assert response.json()['id'] == user.id and response.json()['role'] == 'member'
    finally:
        client.close()
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize('legacy,current', [
    ('welcome', 'model'), ('model', 'model'), ('people', 'usage'),
    ('resources', 'usage'), ('permissions', 'usage'), ('usage', 'usage'), ('try', 'try'),
])
def test_legacy_progress_is_normalized_when_read(legacy, current):
    progress = schemas.OnboardingProgress(status='in_progress', step=legacy, auto_start=True)
    assert progress.step == current
