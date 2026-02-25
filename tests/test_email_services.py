"""Tests for email service."""

import smtplib

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services.domain_settings import notification_settings
from app.services import email as email_service
from tests.mocks import FakeSMTP


def test_send_email_success(db_session, monkeypatch):
    """Test sending email successfully."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "testuser")
    monkeypatch.setenv("SMTP_PASSWORD", "testpass")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="recipient@example.com",
        subject="Test Subject",
        body_html="<p>Hello World</p>",
        body_text="Hello World",
        track=False,
    )

    assert result is True
    assert len(fake_smtp.messages) == 1
    from_addr, to_addrs, msg = fake_smtp.messages[0]
    assert "recipient@example.com" in to_addrs


def test_send_email_html_and_text(db_session, monkeypatch):
    """Test sending email with both HTML and text content."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="user@example.com",
        subject="Multi-part Email",
        body_html="<h1>HTML Content</h1>",
        body_text="Text Content",
        track=False,
    )

    assert result is True
    assert len(fake_smtp.messages) == 1
    _, _, msg = fake_smtp.messages[0]
    assert "HTML Content" in msg or "Text Content" in msg


def test_send_email_with_tracking(db_session, monkeypatch):
    """Test sending email with notification tracking."""
    fake_smtp = FakeSMTP()

    def mock_smtp(*args, **kwargs):
        return fake_smtp

    monkeypatch.setattr("smtplib.SMTP", mock_smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    # When track=True, a notification record should be created
    result = email_service.send_email(
        db=db_session,
        to_email="tracked@example.com",
        subject="Tracked Email",
        body_html="<p>Tracked content</p>",
        body_text="Tracked content",
        track=True,
    )

    assert result is True


def test_get_smtp_config_from_env(monkeypatch):
    """Test getting SMTP config from environment variables."""
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    # Some environments may set SMTP_USERNAME; ensure this test is deterministic.
    monkeypatch.setenv("SMTP_USERNAME", "admin")
    monkeypatch.setenv("SMTP_USER", "admin")
    monkeypatch.setenv("SMTP_PASSWORD", "secret123")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "sender@example.com")
    monkeypatch.setenv("SMTP_FROM", "sender@example.com")
    monkeypatch.setenv("SMTP_TLS", "true")

    config = email_service._get_smtp_config(db=None)

    assert config["host"] == "mail.example.com"
    assert config["port"] == 465
    assert config["user"] == "admin"
    assert config["password"] == "secret123"
    assert config["from_addr"] == "sender@example.com"


def test_send_email_connection_error(db_session, monkeypatch):
    """Test handling SMTP connection error."""
    def mock_smtp_error(*args, **kwargs):
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_error)
    monkeypatch.setenv("SMTP_HOST", "invalid.host")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    result = email_service.send_email(
        db=db_session,
        to_email="user@example.com",
        subject="Test",
        body_html="<p>Test</p>",
        body_text="Test",
        track=False,
    )
    assert result is False


def test_send_email_auth_failure_logs(db_session, monkeypatch, caplog):
    """Test SMTP authentication failure is surfaced in logs."""
    def mock_smtp_auth_error(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_auth_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_auth_error)
    monkeypatch.setenv("SMTP_HOST", "smtp.test.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "noreply@test.local")

    with caplog.at_level("ERROR"):
        result = email_service.send_email(
            db=db_session,
            to_email="user@example.com",
            subject="Test",
            body_html="<p>Test</p>",
            body_text="Test",
            track=False,
        )

    assert result is False
    assert "SMTP authentication failed" in caplog.text


def test_smtp_connection_auth_failure_logs(monkeypatch, caplog):
    """Test SMTP auth failure during connection test is surfaced."""
    def mock_smtp_auth_error(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

    monkeypatch.setattr("smtplib.SMTP", mock_smtp_auth_error)
    monkeypatch.setattr("smtplib.SMTP_SSL", mock_smtp_auth_error)
    config = {
        "host": "smtp.test.local",
        "port": 587,
        "use_ssl": False,
        "use_tls": False,
        "username": "user",
        "password": "pass",
    }

    with caplog.at_level("ERROR"):
        ok, error = email_service.test_smtp_connection(config)

    assert ok is False
    assert error == "SMTP authentication failed"
    assert "SMTP authentication failed during connection test" in caplog.text


def test_get_smtp_config_uses_activity_mapped_sender(db_session):
    """Sender config should be selected from activity mapping when present."""
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.host",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="smtp.billing.local"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.port",
        DomainSettingUpdate(value_type=SettingValueType.integer, value_text="2525"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.username",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="billing-user"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.password",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text="billing-pass",
            is_secret=True,
        ),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.from_email",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="billing@example.com"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_sender.billing.use_tls",
        DomainSettingUpdate(value_type=SettingValueType.boolean, value_text="true", value_json=True),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_default_sender_key",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="default"),
    )
    notification_settings.upsert_by_key(
        db_session,
        "smtp_activity_sender.billing_invoice",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text="billing"),
    )

    config = email_service._get_smtp_config(db_session, activity="billing_invoice")

    assert config["sender_key"] == "billing"
    assert config["host"] == "smtp.billing.local"
    assert config["port"] == 2525
    assert config["username"] == "billing-user"
    assert config["password"] == "billing-pass"
    assert config["from_email"] == "billing@example.com"


def test_get_smtp_config_falls_back_to_legacy_env(monkeypatch):
    """Legacy env config should still work when no sender profiles exist."""
    monkeypatch.setenv("SMTP_HOST", "legacy.smtp.local")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "legacy@example.com")

    config = email_service._get_smtp_config(db=None, activity="billing_invoice")

    assert config["host"] == "legacy.smtp.local"
    assert config["port"] == 587
    assert config["from_email"] == "legacy@example.com"
