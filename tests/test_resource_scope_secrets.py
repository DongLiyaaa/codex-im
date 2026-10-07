"""Department scope and encrypted MCP header secrets for Skill / MCP resources: pure logic, no database."""
import secrets
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import policy, resource_secrets, schemas, service
from app.models import Resource, ResourceDepartment


@pytest.fixture
def session_secret(monkeypatch):
    value = secrets.token_hex(32)
    monkeypatch.setenv('SESSION_SECRET', value)
    return value


def holder(org_id, team_id):
    return SimpleNamespace(org_id=org_id, team_id=team_id)


GLOBAL, ORG, DEPARTMENT = holder(None, None), holder('org', None), holder('org', 'dept')


@pytest.mark.parametrize('resource, subject, allowed', [
    (GLOBAL, holder(None, None), True), (GLOBAL, holder('org', None), True), (GLOBAL, holder('org', 'dept'), True),
    (ORG, holder('org', None), True), (ORG, holder('org', 'dept'), True),
    (ORG, holder('org2', 'dept'), False), (ORG, holder(None, None), False),
    (DEPARTMENT, holder('org', 'dept'), True),
    (DEPARTMENT, holder('org', None), False),      # an organization-level group has no department
    (DEPARTMENT, holder('org', 'dept2'), False),
    (DEPARTMENT, holder('org2', 'dept'), False),   # same department id under another organization
    (DEPARTMENT, holder(None, None), False),
])
def test_resource_scope_matrix(resource, subject, allowed):
    assert policy.in_resource_scope(resource, subject.org_id, subject.team_id) is allowed


def test_resource_without_team_attribute_behaves_as_organization_wide():
    legacy = SimpleNamespace(org_id='org')
    assert policy.in_resource_scope(legacy, 'org', 'any-dept') is True
    assert policy.in_resource_scope(legacy, 'org2', 'any-dept') is False


def test_department_lives_in_a_side_table_row_and_can_be_cleared():
    resource = Resource(id='r', name='n', kind='skill', org_id='org', enabled=True, config={})
    assert resource.team_id is None and resource.department is None
    resource.team_id = 'dept'
    assert isinstance(resource.department, ResourceDepartment) and resource.department.team_id == 'dept'
    assert resource.team_id == 'dept'
    resource.team_id = None
    assert resource.department is None and resource.team_id is None


def test_only_administrators_of_the_owning_organization_manage_a_department_resource():
    resource = Resource(id='r', name='n', kind='skill', org_id='org', enabled=True, config={})
    resource.team_id = 'dept'
    admin = lambda role, org, active=True: SimpleNamespace(role=role, org_id=org, active=active)
    assert policy.can_manage_resource(admin('super_admin', None), resource)
    assert policy.can_manage_resource(admin('org_admin', 'org'), resource)
    assert not policy.can_manage_resource(admin('org_admin', 'org2'), resource)
    assert not policy.can_manage_resource(admin('org_admin', 'org', active=False), resource)
    assert not policy.can_manage_resource(admin('team_lead', 'org'), resource)


def test_create_schema_requires_an_organization_for_a_department():
    base = dict(name='n', kind='skill', config={'content': 'x'})
    with pytest.raises(ValidationError) as error:
        schemas.ResourceCreate(**base, team_id='dept')
    assert '部门范围的资源必须同时指定组织' in str(error.value)
    assert schemas.ResourceCreate(**base, org_id='org', team_id='dept').team_id == 'dept'
    assert schemas.ResourceCreate(**base, org_id='org').team_id is None
    assert schemas.ResourceCreate(**base).org_id is None


def headers():
    return {'Authorization': 'Bearer ' + secrets.token_hex(12), 'X-Api-Key': secrets.token_hex(12)}


def test_header_secrets_round_trip_and_are_not_stored_in_clear(session_secret):
    original = {'url': 'https://mcp.example.com/mcp', 'headers': headers()}
    snapshot = {'url': original['url'], 'headers': dict(original['headers'])}
    sealed = resource_secrets.seal_config('mcp', original)
    assert original == snapshot  # the caller's dict is not modified
    assert sealed['url'] == original['url'] and set(sealed['headers']) == set(original['headers'])
    for name, value in sealed['headers'].items():
        assert value.startswith(resource_secrets.PREFIX)
        assert original['headers'][name] not in value
    assert resource_secrets.reveal_config('mcp', sealed) == original


def test_each_header_is_encrypted_separately(session_secret):
    same = secrets.token_hex(8)
    sealed = resource_secrets.seal_config('mcp', {'url': 'https://mcp.example.com/mcp', 'headers': {'A': same, 'B': same}})
    assert sealed['headers']['A'] != sealed['headers']['B']  # no equal-secret fingerprint


def test_values_saved_before_encryption_existed_keep_working(session_secret):
    legacy = {'url': 'https://mcp.example.com/mcp', 'headers': headers()}
    assert resource_secrets.reveal_config('mcp', legacy) == legacy
    mixed = resource_secrets.seal_config('mcp', {'url': legacy['url'], 'headers': {'New': 'v-new'}})
    mixed['headers']['Old'] = 'v-old'
    assert resource_secrets.reveal_config('mcp', mixed)['headers'] == {'New': 'v-new', 'Old': 'v-old'}


def test_configs_without_header_secrets_need_no_key(monkeypatch):
    monkeypatch.delenv('SESSION_SECRET', raising=False)
    plain = {'url': 'https://mcp.example.com/mcp'}
    assert resource_secrets.seal_config('mcp', plain) == plain
    assert resource_secrets.reveal_config('mcp', plain) == plain
    skill = {'content': '---\nname: "x"\ndescription: "d"\n---\nbody'}
    assert resource_secrets.seal_config('skill', skill) == skill
    assert resource_secrets.reveal_config('skill', skill) == skill
    assert not resource_secrets.has_headers('mcp', plain) and not resource_secrets.has_headers('skill', skill)


def test_changed_or_too_short_key_is_a_clear_error_not_a_leak(monkeypatch, session_secret):
    sealed = resource_secrets.seal_config('mcp', {'url': 'https://mcp.example.com/mcp', 'headers': headers()})
    monkeypatch.setenv('SESSION_SECRET', secrets.token_hex(32))
    with pytest.raises(HTTPException) as error:
        resource_secrets.reveal_config('mcp', sealed)
    assert error.value.status_code == 503 and 'SESSION_SECRET' in error.value.detail
    assert resource_secrets.PREFIX not in error.value.detail
    monkeypatch.setenv('SESSION_SECRET', 'short')
    for action in (resource_secrets.seal_config, resource_secrets.reveal_config):
        with pytest.raises(HTTPException) as short:
            action('mcp', sealed if action is resource_secrets.reveal_config else {'url': 'u', 'headers': {'A': 'b'}})
        assert short.value.status_code == 503


def test_a_tampered_value_is_rejected(session_secret):
    sealed = resource_secrets.seal_config('mcp', {'url': 'https://mcp.example.com/mcp', 'headers': {'A': secrets.token_hex(8)}})
    token = sealed['headers']['A']
    sealed['headers']['A'] = token[:-4] + ('AAAA' if not token.endswith('AAAA') else 'BBBB')
    with pytest.raises(HTTPException) as error:
        resource_secrets.reveal_config('mcp', sealed)
    assert error.value.status_code == 503


def test_stored_value_is_still_a_valid_header_for_the_runner_check(monkeypatch, session_secret):
    monkeypatch.setattr(service, 'validate_mcp_url', lambda url: None)  # the address check resolves DNS; only headers matter here
    config = {'url': 'https://mcp.example.com/mcp', 'headers': {'Authorization': secrets.token_hex(20)}}
    service.validate_resource('mcp', config)
    sealed = resource_secrets.seal_config('mcp', config)
    service.validate_resource('mcp', resource_secrets.reveal_config('mcp', sealed))


def test_listing_never_returns_header_values_to_anyone(session_secret):
    resource = Resource(id='r', name='n', kind='mcp', description='', org_id='org', enabled=True,
        config=resource_secrets.seal_config('mcp', {'url': 'https://mcp.example.com/mcp', 'headers': headers()}))
    resource.team_id = 'dept'
    manager = SimpleNamespace(role='org_admin', org_id='org', active=True)
    shown = service.resource_out(resource, manager)
    assert shown['config'] == {'url': 'https://mcp.example.com/mcp', 'headers': {'Authorization': '***', 'X-Api-Key': '***'}}
    assert shown['team_id'] == 'dept' and shown['org_id'] == 'org'
    employee = SimpleNamespace(role='member', org_id='org', active=True)
    assert service.resource_out(resource, employee)['config'] == {}
    assert resource_secrets.PREFIX not in str(shown)


def test_skill_text_is_visible_to_members_but_mcp_connection_details_are_not(session_secret):
    skill = Resource(id='s', name='n', kind='skill', description='', org_id=None, enabled=True, config={'content': 'text'})
    member = SimpleNamespace(role='member', org_id='org', active=True)
    assert service.resource_out(skill, member)['config'] == {'content': 'text'}
