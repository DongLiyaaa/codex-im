from types import SimpleNamespace

import pytest

from app import im_settings as settings


@pytest.fixture(autouse=True)
def clear_im_environment(monkeypatch):
    for provider in settings.FIELDS:
        for field in settings.FIELDS[provider] + ['TRANSPORT']:
            monkeypatch.delenv(f'{provider.upper()}_{field}', raising=False)


def empty_db():
    return SimpleNamespace(get=lambda *args: None)


@pytest.mark.parametrize('provider,transport', [('feishu', 'websocket'), ('dingtalk', 'stream')])
def test_fresh_empty_configuration_prefers_long_connection(provider, transport):
    values, revision = settings.effective(empty_db(), provider)
    assert values[f'{provider.upper()}_TRANSPORT'] == transport
    assert revision == 0


@pytest.mark.parametrize('provider,field', [('feishu', 'APP_ID'), ('dingtalk', 'CLIENT_ID')])
def test_any_existing_environment_field_keeps_legacy_webhook_default(monkeypatch, provider, field):
    monkeypatch.setenv(f'{provider.upper()}_{field}', 'existing-public-id')
    values, _ = settings.effective(empty_db(), provider)
    assert values[f'{provider.upper()}_TRANSPORT'] == 'webhook'


@pytest.mark.parametrize('provider,field', [('feishu', 'APP_ID'), ('dingtalk', 'CLIENT_ID')])
def test_any_existing_database_field_keeps_legacy_webhook_default(monkeypatch, provider, field):
    monkeypatch.setattr(settings, 'read', lambda db, name: ({field: 'existing-public-id'}, 1))
    values, _ = settings.effective(empty_db(), provider)
    assert values[f'{provider.upper()}_TRANSPORT'] == 'webhook'


@pytest.mark.parametrize('provider,transport', [('feishu', 'webhook'), ('dingtalk', 'stream')])
def test_explicit_environment_transport_is_unchanged(monkeypatch, provider, transport):
    monkeypatch.setenv(f'{provider.upper()}_TRANSPORT', transport)
    values, _ = settings.effective(empty_db(), provider)
    assert values[f'{provider.upper()}_TRANSPORT'] == transport


@pytest.mark.parametrize('provider,transport', [('feishu', 'websocket'), ('dingtalk', 'webhook')])
def test_explicit_database_transport_is_unchanged(monkeypatch, provider, transport):
    monkeypatch.setattr(settings, 'read', lambda db, name: ({'TRANSPORT': transport}, 1))
    values, _ = settings.effective(empty_db(), provider)
    assert values[f'{provider.upper()}_TRANSPORT'] == transport
