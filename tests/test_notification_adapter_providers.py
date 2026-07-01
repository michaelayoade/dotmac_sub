"""EmailProvider/SmsProvider must invoke the real senders with their actual
signatures (they used to pass wrong kwargs and silently swallow the TypeError)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.services.notification_adapter import (
    EmailProvider,
    NotificationRequest,
    SmsProvider,
)

_SMS_ENV_KEYS = (
    "SMS_ENABLED",
    "SMS_PROVIDER",
    "SMS_API_KEY",
    "SMS_API_SECRET",
    "SMS_FROM_NUMBER",
    "SMS_WEBHOOK_URL",
)


def test_email_provider_send_invokes_send_email_correctly(monkeypatch):
    captured: dict = {}

    def _fake_send_email(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("app.services.email.send_email", _fake_send_email)

    result = EmailProvider().send(
        NotificationRequest(
            channel=NotificationChannel.email,
            recipient="ops@example.com",
            message="Provisioning complete",
            title="ONT provisioned",
        )
    )

    assert result.success is True
    assert captured["to_email"] == "ops@example.com"
    assert isinstance(captured["db"], Session)
    assert captured["body_text"] == "Provisioning complete"
    assert "<!DOCTYPE html>" in captured["body_html"]
    assert "ONT provisioned" in captured["body_html"]


def test_email_provider_send_reports_failure(monkeypatch):
    monkeypatch.setattr("app.services.email.send_email", lambda **_: False)

    result = EmailProvider().send(
        NotificationRequest(
            channel=NotificationChannel.email,
            recipient="ops@example.com",
            message="hello",
        )
    )

    assert result.success is False
    assert result.error == "send_failed"


def test_sms_provider_send_invokes_send_sms_correctly(monkeypatch):
    captured: dict = {}

    def _fake_send_sms(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr("app.services.sms.send_sms", _fake_send_sms)

    long_message = "x" * 200
    result = SmsProvider().send(
        NotificationRequest(
            channel=NotificationChannel.sms,
            recipient="+2348000000001",
            message=long_message,
        )
    )

    assert result.success is True
    assert captured["to_phone"] == "+2348000000001"
    assert isinstance(captured["db"], Session)
    assert captured["body"] == long_message


def test_sms_provider_unavailable_without_provider_config(monkeypatch):
    for key in _SMS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Enabled (via env, so it doesn't depend on DB state) but no provider
    # credentials configured -> must report unavailable.
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_PROVIDER", "twilio")

    assert SmsProvider().is_available() is False


def test_sms_provider_available_with_webhook_config(monkeypatch):
    for key in _SMS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SMS_ENABLED", "true")
    monkeypatch.setenv("SMS_PROVIDER", "webhook")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.test/send")

    assert SmsProvider().is_available() is True


def test_sms_provider_unavailable_when_disabled(monkeypatch):
    for key in _SMS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SMS_ENABLED", "false")
    monkeypatch.setenv("SMS_PROVIDER", "webhook")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.test/send")

    assert SmsProvider().is_available() is False
