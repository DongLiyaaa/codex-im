from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main, policy, schemas, service, directory
from app.models import Binding


@pytest.fixture
def setup(monkeypatch):
    binding = Binding(id='b', subject_type='user', subject_id='old', resource_id='r')
    db = SimpleNamespace(scalar=lambda query: binding, flush=lambda: None)
    calls = []
    monkeypatch.setattr(directory, 'scope_lock', lambda db: None)
    monkeypatch.setattr(directory, 'validate_scope', lambda *args: None)
    monkeypatch.setattr(main, 'get_or_404', lambda *args: binding)
    monkeypatch.setattr(policy, 'can_manage_binding', lambda *args: True)
    monkeypatch.setattr(policy, 'binding_subject', lambda *args: SimpleNamespace(org_id='org', team_id='team', active=True))
    monkeypatch.setattr(service, 'audit', lambda *args: calls.append(args))
    return binding, db, calls


def test_update_preserves_binding_id_and_audits_before_and_after(setup):
    binding, db, calls = setup
    results = iter([binding, None])
    db.scalar = lambda query: next(results)
    result = main.update_binding('b', schemas.BindingCreate(subject_type='team', subject_id='new', resource_id='r2'), object(), db)
    assert result['id'] == 'b' and result['can_manage']
    assert (binding.subject_type, binding.subject_id, binding.resource_id) == ('team', 'new', 'r2')
    assert calls[0][2] == 'binding.update'
    assert calls[0][4] == {'previous': {'subject_type': 'user', 'subject_id': 'old', 'resource_id': 'r'},
                           'current': {'subject_type': 'team', 'subject_id': 'new', 'resource_id': 'r2'}}


@pytest.mark.parametrize('deny_old', [True, False])
def test_both_old_and_new_bindings_require_management_permission(setup, monkeypatch, deny_old):
    binding, db, calls = setup
    monkeypatch.setattr(policy, 'can_manage_binding', lambda db, actor, b: (b.subject_id != 'old') if deny_old else (b.subject_id == 'old'))
    with pytest.raises(HTTPException) as exc:
        main.update_binding('b', schemas.BindingCreate(subject_type='user', subject_id='new', resource_id='r'), object(), db)
    assert exc.value.status_code == 403
    assert binding.subject_id == 'old' and not calls


def test_duplicate_binding_does_not_change_old_values(setup):
    binding, db, calls = setup
    results = iter([binding, 'duplicate'])
    db.scalar = lambda query: next(results)
    with pytest.raises(HTTPException) as exc:
        main.update_binding('b', schemas.BindingCreate(subject_type='user', subject_id='new', resource_id='r'), object(), db)
    assert exc.value.status_code == 409
    assert binding.subject_id == 'old' and not calls


def test_noop_keeps_existing_binding_and_no_audit(setup):
    binding, db, calls = setup
    main.update_binding('b', schemas.BindingCreate(subject_type='user', subject_id='old', resource_id='r'), object(), db)
    assert not calls


def test_disabled_target_is_rejected(setup, monkeypatch):
    binding, db, calls = setup
    monkeypatch.setattr(policy, 'binding_subject', lambda *args: SimpleNamespace(org_id='org', team_id='team', active=False))
    with pytest.raises(HTTPException) as exc:
        main.update_binding('b', schemas.BindingCreate(subject_type='user', subject_id='new', resource_id='r'), object(), db)
    assert exc.value.status_code == 409 and binding.subject_id == 'old'


def test_group_locks_are_sorted_before_binding_lock(setup, monkeypatch):
    binding, db, calls = setup
    binding.subject_type, binding.subject_id = 'group', 'z'
    order = []
    monkeypatch.setattr(service, 'lock_group', lambda db, identifier: order.append(identifier))
    results = iter([binding, None])
    def scalar(query):
        order.append('binding')
        return next(results)
    db.scalar = scalar
    main.update_binding('b', schemas.BindingCreate(subject_type='group', subject_id='a', resource_id='r'), object(), db)
    assert order[:3] == ['a', 'z', 'binding']


def test_concurrently_changed_binding_is_not_overwritten(setup):
    binding, db, calls = setup
    def refreshed(query):
        binding.subject_id = 'changed'
        return binding
    db.scalar = refreshed
    with pytest.raises(HTTPException) as exc:
        main.update_binding('b', schemas.BindingCreate(subject_type='user', subject_id='new', resource_id='r'), object(), db)
    assert exc.value.status_code == 409 and binding.subject_id == 'changed' and not calls
