"""Tests for email service."""

import smtplib

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
