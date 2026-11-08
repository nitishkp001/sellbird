"""Relay delivery routes replies without pretending an inbox was monitored."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from harvey.config import EmailChannelConfig, EnvConfig
from harvey.integrations.smtp_mail import SmtpImapProvider


def provider(send_only=True):
    config = SimpleNamespace(
        persona=SimpleNamespace(name="Sender", email="sender@example.com"),
        channels=SimpleNamespace(email=EmailChannelConfig(
            provider="smtp", smtp_send_only=send_only, reply_to="replies@example.org")),
    )
    return SmtpImapProvider(config, EnvConfig(
        smtp_host="smtp.example.com", smtp_username="relay", smtp_password="fake"))


@pytest.mark.asyncio
async def test_reply_to_routes_replies_without_cc(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.send", send)
    result = await provider().send_email("recipient@example.net", "Hello", "A message")
    assert result.ok
    message = send.call_args.args[0]
    assert str(message["Reply-To"]) == "replies@example.org"
    assert message["Cc"] is None
    assert "sender@example.com" in str(message["From"])


@pytest.mark.asyncio
async def test_send_only_health_and_poll_never_connect_imap(monkeypatch):
    smtp = SimpleNamespace(connect=AsyncMock(), login=AsyncMock(), quit=AsyncMock())
    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.SMTP", lambda **kw: smtp)
    imap = Mock(side_effect=AssertionError("No inbox in send-only mode"))
    monkeypatch.setattr("harvey.integrations.smtp_mail.imaplib.IMAP4_SSL", imap)
    relay = provider()
    ok, detail = await relay.test_connection()
    assert ok and "send-only" in detail
    assert await relay.get_replies() == []
    smtp.login.assert_awaited_once_with("relay", "fake")
    imap.assert_not_called()


@pytest.mark.asyncio
async def test_normal_mailbox_still_checks_imap(monkeypatch):
    smtp = SimpleNamespace(connect=AsyncMock(), login=AsyncMock(), quit=AsyncMock())
    monkeypatch.setattr("harvey.integrations.smtp_mail.aiosmtplib.SMTP", lambda **kw: smtp)
    inbox = Mock()
    imap = Mock(return_value=inbox)
    monkeypatch.setattr("harvey.integrations.smtp_mail.imaplib.IMAP4_SSL", imap)
    relay = provider(send_only=False)
    ok, detail = await relay.test_connection()
    assert ok and "SMTP + IMAP" in detail
    imap.assert_called_once()
    inbox.login.assert_called_once_with("relay", "fake")
    inbox.logout.assert_called_once()


@pytest.mark.parametrize("change", [
    {"reply_to": ""}, {"require_approval": False}, {"provider": "gmail"},
    {"reply_to": "replies@example.org\r\nBcc: other@example.net"},
])
def test_send_only_rejects_invalid_configuration(change):
    values = dict(provider="smtp", smtp_send_only=True, reply_to="replies@example.org")
    values.update(change)
    with pytest.raises(ValidationError):
        EmailChannelConfig(**values)
