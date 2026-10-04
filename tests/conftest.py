"""Shared fixtures: inbound preparation must never reach the real Feishu API from tests."""
import pytest

BOT_OPEN_ID = 'ou_test_bot'


@pytest.fixture(autouse=True)
def offline_inbound(monkeypatch):
    from app import im_inbound
    monkeypatch.setattr(im_inbound, 'bot_open_id', lambda: BOT_OPEN_ID)
    monkeypatch.setattr(im_inbound, 'fetch_message', lambda message_id, deadline: None)
