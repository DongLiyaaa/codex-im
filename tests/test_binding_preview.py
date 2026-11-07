from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import binding_preview as preview, policy
from app.models import Binding, Group, Resource, User


def resource(identifier, enabled=True, org='org'):
    return Resource(id=identifier, name=identifier, kind='skill', enabled=enabled, org_id=org,
                    config={'content': 'PRIVATE-SOURCE', 'headers': {'Authorization': 'PRIVATE-KEY'}})


class Store:
    def __init__(self, users, groups, resources, bindings):
        self.people = {u.id: u for u in users}
        self.groups = {g.id: g for g in groups}
        self.resources, self.bindings = resources, bindings
    def get(self, model, identifier):
        return (self.people if model is User else self.groups if model is Group else {}).get(identifier)
    def scalars(self, query):
        entity = query.column_descriptions[0]['entity']
        return self.resources if entity is Resource else self.bindings


def fixture(monkeypatch, *, user_active=True, member=True, group_org='org', disabled=False, user_grant=True, group_grant=True):
    actor = SimpleNamespace(id='admin', role='org_admin', active=True, org_id='org', team_id=None)
    user = SimpleNamespace(id='member', role='member', active=user_active, org_id='org', team_id='team')
    group = SimpleNamespace(id='group', org_id=group_org, team_id='team', member_ids=['member'] if member else [], archived_at=None)
    bindings = []
    if user_grant: bindings.append(Binding(subject_type='team', subject_id='team', resource_id='skill'))
    if group_grant: bindings.append(Binding(subject_type='group', subject_id='group', resource_id='skill'))
    db = Store([actor, user], [group], [resource('skill', enabled=not disabled), resource('foreign', org='other')], bindings)
    monkeypatch.setattr(policy, 'subject_keys', lambda db, owner, kind: [(kind, owner.id), ('team', 'team')] if kind == 'user' else [(kind, owner.id)])
    monkeypatch.setattr(policy, 'granted_ids', lambda db, keys: {b.resource_id for b in db.bindings if (b.subject_type, b.subject_id) in keys})
    return db, actor, user, group


@pytest.mark.parametrize('user_grant,group_grant,disabled,reason', [
    (True, True, False, None), (False, True, False, 'user_not_granted'),
    (True, False, False, 'group_not_granted'), (True, True, True, 'disabled'),
])
def test_preview_matches_actual_execution_and_does_not_expose_other_tenant_or_config(monkeypatch, user_grant, group_grant, disabled, reason):
    db, actor, user, group = fixture(monkeypatch, user_grant=user_grant, group_grant=group_grant, disabled=disabled)
    # Real group visibility checks are exercised separately; this test isolates resource decisions.
    monkeypatch.setattr(policy, 'can_read_group', lambda *args: True)
    result = preview.evaluate(db, actor, user.id, group.id)
    context = SimpleNamespace(owner_id=user.id, group_id=group.id, archived_at=None)
    effective = policy.effective_resources(db, user, context)
    # Store returns all resources irrespective of SQL filters; emulate the SQL candidate filter here.
    expected = [r.id for r in effective if r.id in policy.granted_ids(db, policy.subject_keys(db, user, 'user')) & policy.granted_ids(db, policy.subject_keys(db, group, 'group')) and r.enabled]
    assert [r.id for r in result.resources if r.effective] == expected
    assert [r.id for r in result.resources] == ['skill']
    assert reason is None or reason in result.resources[0].reasons
    text = result.model_dump_json()
    assert 'PRIVATE' not in text and 'config' not in text and 'foreign' not in text


def test_preview_rejects_cross_tenant_user_and_non_admin(monkeypatch):
    db, actor, user, group = fixture(monkeypatch)
    user.org_id = 'other'
    with pytest.raises(HTTPException) as exc: preview.evaluate(db, actor, user.id)
    assert exc.value.status_code == 404
    actor.role = 'member'
    with pytest.raises(HTTPException) as exc: preview.evaluate(db, actor, actor.id)
    assert exc.value.status_code == 403


def test_group_cannot_be_enumerated_without_view_permission(monkeypatch):
    db, actor, user, group = fixture(monkeypatch, group_org='other')
    with pytest.raises(HTTPException) as exc: preview.evaluate(db, actor, user.id, group.id)
    assert exc.value.status_code == 404


@pytest.mark.parametrize('active,member,reason', [(False, True, 'inactive_user'), (True, False, 'not_group_member')])
def test_context_blocks_effective_resources(monkeypatch, active, member, reason):
    db, actor, user, group = fixture(monkeypatch, user_active=active, member=member)
    monkeypatch.setattr(policy, 'can_read_group', lambda *args: True)
    result = preview.evaluate(db, actor, user.id, group.id)
    assert not result.can_send and reason in result.context_reasons
    assert all(not r.effective and 'cannot_send' in r.reasons for r in result.resources)


def test_private_preview_includes_department_source(monkeypatch):
    db, actor, user, group = fixture(monkeypatch)
    result = preview.evaluate(db, actor, user.id)
    assert result.resources[0].effective
    assert [(s.side, s.subject_type) for s in result.resources[0].sources] == [('user', 'team')]
