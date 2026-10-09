"""Agents a conversation can be bound to. Codex CLI is always available; Claude CLI is an additional, opt-in choice.

The enabled set comes from HUB_AGENTS (comma separated, default "codex"), the same variable the runner reads.
A conversation keeps its agent for life: switching starts a new conversation, so contexts are never mixed.
"""
import os

from fastapi import HTTPException

AGENTS = ('codex', 'claude')
LABELS = {'codex': 'Codex CLI', 'claude': 'Claude CLI'}


def enabled():
    names = [part.strip().lower() for part in os.getenv('HUB_AGENTS', 'codex').split(',') if part.strip()]
    result = [name for name in AGENTS if name in names]
    return result or ['codex']


def default():
    name = os.getenv('DEFAULT_AGENT', 'codex').strip().lower()
    return name if name in enabled() else enabled()[0]


def resolve(user):
    """The agent for a new conversation: the person's own choice while it is enabled, else the Hub default."""
    preferred = getattr(user, 'preferred_agent', None)
    return preferred if preferred in enabled() else default()


def require_enabled(name):
    if name not in enabled():
        raise HTTPException(422, f'{LABELS.get(name, name)} 未启用，请联系管理员或新建会话选择其他 Agent。')
    return name


def options():
    return [{'id': name, 'label': LABELS[name]} for name in enabled()]
