"""Capability identity selection without requiring a database container."""
from types import SimpleNamespace
from app import main
from app.models import User


class FakeDb:
    def __init__(self, owner):
        self.owner = owner

    def get(self, model, identifier):
        assert model is User
        return self.owner if identifier == self.owner.id else None


def test_readonly_supervision_counts_the_conversation_owner_capabilities(monkeypatch):
    actor = SimpleNamespace(id='admin')
    owner = SimpleNamespace(id='owner')
    conversation = SimpleNamespace(id='c', owner_id=owner.id, group_id=None)
    monkeypatch.setattr(main, 'read_conversation', lambda db, user, identifier: conversation)
    monkeypatch.setattr(main.policy, 'can_send_conversation', lambda db, user, item: False)
    monkeypatch.setattr(main.policy, 'effective_resources', lambda db, user, item: [
        SimpleNamespace(kind='skill', name='Skill') if user is owner else SimpleNamespace(kind='mcp', name='Wrong subject')
    ])
    monkeypatch.setattr(main.service, 'resource_out', lambda resource, user: {'name': resource.name})
    result = main.capabilities(conversation.id, actor, FakeDb(owner))
    assert result == {'skills': [{'name': 'Skill'}], 'mcps': []}


def test_active_sender_keeps_their_own_group_capability_context(monkeypatch):
    actor = SimpleNamespace(id='sender')
    owner = SimpleNamespace(id='owner')
    conversation = SimpleNamespace(id='c', owner_id=owner.id, group_id='g')
    monkeypatch.setattr(main, 'read_conversation', lambda db, user, identifier: conversation)
    monkeypatch.setattr(main.policy, 'can_send_conversation', lambda db, user, item: True)
    monkeypatch.setattr(main.policy, 'effective_resources', lambda db, user, item: [
        SimpleNamespace(kind='mcp', name='MCP') if user is actor else SimpleNamespace(kind='skill', name='Wrong subject')
    ])
    monkeypatch.setattr(main.service, 'resource_out', lambda resource, user: {'name': resource.name})
    result = main.capabilities(conversation.id, actor, FakeDb(owner))
    assert result == {'skills': [], 'mcps': [{'name': 'MCP'}]}
