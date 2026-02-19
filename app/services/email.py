import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env_value(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_value(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _setting_bool(db: Session | None, key: str, default: bool) -> bool:
    value = _setting_value(db, key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_value(db: Session | None, key: str) -> str | None:
    if db is None:
        return None
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _get_smtp_config(db: Session | None) -> dict:
    username = _env_value("SMTP_USERNAME") or _env_value("SMTP_USER") or _setting_value(
        db, "smtp_username"
    )
    from_email = (
        _env_value("SMTP_FROM_EMAIL")
        or _env_value("SMTP_FROM")
        or _setting_value(db, "smtp_from_email")
        or "noreply@example.com"
    )
    default_tls = _setting_bool(db, "smtp_use_tls", True)
    default_ssl = _setting_bool(db, "smtp_use_ssl", False)
    use_tls = _env_bool("SMTP_USE_TLS", _env_bool("SMTP_TLS", default_tls))
    use_ssl = _env_bool("SMTP_USE_SSL", _env_bool("SMTP_SSL", default_ssl))
    return {
        "host": _env_value("SMTP_HOST") or _setting_value(db, "smtp_host") or "localhost",
        "port": _env_int("SMTP_PORT", 587),
        "username": username,
        "password": _env_value("SMTP_PASSWORD") or _setting_value(db, "smtp_password"),
        "use_tls": use_tls,
        "use_ssl": use_ssl,
        "from_email": from_email,
        "from_name": _env_value("SMTP_FROM_NAME") or _setting_value(db, "smtp_from_name") or "DotMac SM",
        "user": username,
        "from_addr": from_email,
    }


def _get_app_url(db: Session | None) -> str:
    return _env_value("APP_URL") or _setting_value(db, "app_url") or "http://localhost:8000"


def _create_smtp_client(host: str, port: int, use_ssl: bool, timeout: int | None = None):
    if use_ssl:
        if timeout is None:
            return smtplib.SMTP_SSL(host, port)
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    if timeout is None:
        return smtplib.SMTP(host, port)
    return smtplib.SMTP(host, port, timeout=timeout)


def send_email_with_config(
    config: dict,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.get('from_name', 'DotMac SM')} <{config.get('from_email', 'noreply@example.com')}>"
    msg["To"] = to_email

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        host = str(config.get("host") or "")
        if not host:
            return False
        port = int(config.get("port", 587) or 587)
        server = _create_smtp_client(host, port, bool(config.get("use_ssl")))

        if config.get("use_tls") and not config.get("use_ssl"):
            server.starttls()

        username = config.get("username")
        password = config.get("password")
        if username and password:
            server.login(username, password)

        server.sendmail(config.get("from_email"), to_email, msg.as_string())
        server.quit()
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed for %s: %s", to_email, exc)
        return False
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


def send_email(
    db: Session | None,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    track: bool = True,
) -> bool:
    """
    Send an email via SMTP.

    Args:
        db: Database session for settings lookup and notification tracking
        to_email: Recipient email address
        subject: Email subject
        body_html: HTML body content
        body_text: Plain text body (optional, derived from HTML if not provided)
        track: Whether to create a Notification record for tracking

    Returns:
        True if email was sent successfully, False otherwise
    """
    config = _get_smtp_config(db)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config['from_name']} <{config['from_email']}>"
    msg["To"] = to_email

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    notification = None
    if track and db:
        notification = Notification(
            channel=NotificationChannel.email,
            recipient=to_email,
            subject=subject,
            body=body_html,
            status=NotificationStatus.sending,
        )
        db.add(notification)
        db.commit()
        db.refresh(notification)

    try:
        server = _create_smtp_client(
            config["host"],
            config["port"],
            bool(config["use_ssl"]),
        )

        if config["use_tls"] and not config["use_ssl"]:
            server.starttls()

        if config["username"] and config["password"]:
            server.login(config["username"], config["password"])

        server.sendmail(config["from_email"], to_email, msg.as_string())
        server.quit()

        if notification:
            assert db is not None
            notification.status = NotificationStatus.delivered
            db.commit()

        logger.info(f"Email sent successfully to {to_email}")
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed for %s: %s", to_email, exc)
        if notification:
            assert db is not None
            notification.status = NotificationStatus.failed
            notification.last_error = "SMTP authentication failed"
            db.commit()
        return False
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        if notification:
            assert db is not None
            notification.status = NotificationStatus.failed
            notification.last_error = str(e)
            db.commit()
        return False


def test_smtp_connection(
    config: dict,
    timeout_sec: int | None = None,
    db: Session | None = None,
) -> tuple[bool, str | None]:
    host = config.get("host")
    if not host:
        return False, "SMTP host is required"

    # Use configurable timeout, fallback to default of 10 seconds
    if timeout_sec is None:
        raw_timeout = (
            resolve_value(db, SettingDomain.notification, "smtp_test_timeout_seconds")
            if db
            else None
        )
        if raw_timeout is None:
            timeout_sec = 10
        else:
            try:
                if isinstance(raw_timeout, int):
                    timeout_sec = raw_timeout
                elif isinstance(raw_timeout, float):
                    timeout_sec = int(raw_timeout)
                elif isinstance(raw_timeout, str):
                    timeout_sec = int(raw_timeout)
                else:
                    raise TypeError("Unsupported timeout type")
            except (TypeError, ValueError):
                timeout_sec = 10

    server = None
    try:
        port = config.get("port", 587)
        use_ssl = bool(config.get("use_ssl"))
        use_tls = bool(config.get("use_tls"))

        server = _create_smtp_client(host, port, use_ssl, timeout=timeout_sec)

        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()

        username = config.get("username")
        password = config.get("password")
        if username and password:
            server.login(username, password)

        server.noop()
        return True, None
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed during connection test: %s", exc)
        return False, "SMTP authentication failed"
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as exc:
        return False, f"SMTP connection failed: {exc}"
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def send_password_reset_email(db: Session, to_email: str, reset_token: str, person_name: str | None = None) -> bool:
    """
    Send a password reset email.

    Args:
        db: Database session
        to_email: Recipient email address
        reset_token: The JWT reset token
        person_name: Optional name to personalize the email

    Returns:
        True if email was sent successfully, False otherwise
    """
    app_url = _get_app_url(db)
    reset_url = f"{app_url}/auth/reset-password?token={reset_token}"

    # Get configurable expiry minutes
    expiry_minutes = resolve_value(db, SettingDomain.auth, "password_reset_expiry_minutes") or 60

    greeting = f"Hi {person_name}," if person_name else "Hi,"

    subject = "Password Reset Request"

    body_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .button {{
            display: inline-block;
            padding: 12px 24px;
            background-color: #007bff;
            color: #ffffff;
            text-decoration: none;
            border-radius: 4px;
            margin: 20px 0;
        }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Password Reset Request</h2>
        <p>{greeting}</p>
        <p>We received a request to reset your password. Click the button below to create a new password:</p>
        <p><a href="{reset_url}" class="button">Reset Password</a></p>
        <p>Or copy and paste this link into your browser:</p>
        <p><a href="{reset_url}">{reset_url}</a></p>
        <p>This link will expire in {expiry_minutes} minutes.</p>
        <p>If you didn't request a password reset, you can safely ignore this email.</p>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
"""

    body_text = f"""{greeting}

We received a request to reset your password.

Click the link below to create a new password:
{reset_url}

This link will expire in {expiry_minutes} minutes.

If you didn't request a password reset, you can safely ignore this email.

This is an automated message. Please do not reply to this email.
"""

    return send_email(db, to_email, subject, body_html, body_text)


def send_user_invite_email(db: Session, to_email: str, reset_token: str, person_name: str | None = None) -> bool:
    """
    Send a new user invitation email.

    Args:
        db: Database session
        to_email: Recipient email address
        reset_token: The JWT reset token
        person_name: Optional name to personalize the email

    Returns:
        True if email was sent successfully, False otherwise
    """
    app_url = _get_app_url(db)
    reset_url = f"{app_url}/auth/reset-password?token={reset_token}"

    # Get configurable expiry minutes
    expiry_minutes = resolve_value(db, SettingDomain.auth, "user_invite_expiry_minutes") or 60

    greeting = f"Hi {person_name}," if person_name else "Hi,"

    subject = "You're invited to Dotmac SM"

    body_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .button {{
            display: inline-block;
            padding: 12px 24px;
            background-color: #007bff;
            color: #ffffff;
            text-decoration: none;
            border-radius: 4px;
            margin: 20px 0;
        }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Welcome to Dotmac SM</h2>
        <p>{greeting}</p>
        <p>Your account has been created. Use the button below to set your password and get started:</p>
        <p><a href="{reset_url}" class="button">Set Password</a></p>
        <p>Or copy and paste this link into your browser:</p>
        <p><a href="{reset_url}">{reset_url}</a></p>
        <p>This link will expire in {expiry_minutes} minutes.</p>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
"""

    body_text = f"""{greeting}

Welcome to Dotmac SM.

Use the link below to set your password:
{reset_url}

This link will expire in {expiry_minutes} minutes.

This is an automated message. Please do not reply to this email.
"""

    return send_email(db, to_email, subject, body_html, body_text)
