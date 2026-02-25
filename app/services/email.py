import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.schemas.settings import DomainSettingUpdate
from app.services.domain_settings import notification_settings
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

SMTP_DEFAULT_SENDER_KEY_SETTING = "smtp_default_sender_key"
SMTP_SENDER_KEY_PREFIX = "smtp_sender."
SMTP_ACTIVITY_KEY_PREFIX = "smtp_activity_sender."
SMTP_SENDER_ALLOWED_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
SMTP_ACTIVITY_CHOICES: list[tuple[str, str]] = [
    ("notification_queue", "Notification Queue"),
    ("notification_test", "Notification Template Tests"),
    ("billing_invoice", "Billing Invoices"),
    ("subscription_welcome", "Subscription Welcome"),
    ("auth_password_reset", "Password Reset"),
    ("auth_user_invite", "User Invite"),
]


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


def _legacy_smtp_config(db: Session | None) -> dict:
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


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _setting_raw_value(setting: DomainSetting) -> Any:
    if setting.value_text is not None:
        return setting.value_text
    return setting.value_json


def _sender_setting_key(sender_key: str, field: str) -> str:
    return f"{SMTP_SENDER_KEY_PREFIX}{sender_key}.{field}"


def _valid_sender_key(sender_key: str) -> bool:
    normalized = sender_key.strip().lower()
    if not normalized:
        return False
    return all(ch in SMTP_SENDER_ALLOWED_KEY_CHARS for ch in normalized)


def list_smtp_senders(db: Session | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.key.like(f"{SMTP_SENDER_KEY_PREFIX}%"))
        .all()
    )

    senders: dict[str, dict[str, Any]] = {}
    for setting in settings:
        parts = setting.key.split(".", 2)
        if len(parts) != 3:
            continue
        _, sender_key, field = parts
        if not sender_key:
            continue
        profile = senders.setdefault(
            sender_key,
            {
                "sender_key": sender_key,
                "host": "",
                "port": 587,
                "username": "",
                "password": None,
                "has_password": False,
                "from_email": "",
                "from_name": "DotMac SM",
                "use_tls": True,
                "use_ssl": False,
                "is_active": True,
            },
        )
        raw = _setting_raw_value(setting)
        if field == "password":
            profile["has_password"] = bool(raw)
            continue
        if field == "port":
            profile["port"] = _coerce_int(raw, 587)
        elif field in {"use_tls", "use_ssl", "is_active"}:
            profile[field] = _coerce_bool(raw, field != "use_ssl")
        elif field in {"host", "username", "from_email", "from_name"}:
            profile[field] = "" if raw is None else str(raw)

    return [senders[key] for key in sorted(senders.keys())]


def get_default_smtp_sender_key(db: Session | None) -> str:
    if db is None:
        return "default"
    raw = _setting_value(db, SMTP_DEFAULT_SENDER_KEY_SETTING)
    key = (raw or "").strip().lower()
    return key or "default"


def get_smtp_activity_map(db: Session | None) -> dict[str, str]:
    if db is None:
        return {}
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.key.like(f"{SMTP_ACTIVITY_KEY_PREFIX}%"))
        .all()
    )
    activity_map: dict[str, str] = {}
    for setting in settings:
        activity = setting.key.replace(SMTP_ACTIVITY_KEY_PREFIX, "", 1).strip()
        if not activity:
            continue
        value = _setting_raw_value(setting)
        if value is None:
            continue
        sender_key = str(value).strip().lower()
        if sender_key:
            activity_map[activity] = sender_key
    return activity_map


def set_default_smtp_sender_key(db: Session, sender_key: str) -> None:
    notification_settings.upsert_by_key(
        db,
        SMTP_DEFAULT_SENDER_KEY_SETTING,
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=sender_key.strip().lower(),
            is_secret=False,
            is_active=True,
        ),
    )


def upsert_smtp_activity_mapping(db: Session, activity: str, sender_key: str | None) -> None:
    key = f"{SMTP_ACTIVITY_KEY_PREFIX}{activity.strip()}"
    normalized = (sender_key or "").strip().lower()
    if normalized:
        notification_settings.upsert_by_key(
            db,
            key,
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=normalized,
                is_active=True,
            ),
        )
        return
    existing = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .first()
    )
    if existing:
        notification_settings.upsert_by_key(db, key, DomainSettingUpdate(is_active=False))


def upsert_smtp_sender(
    db: Session,
    *,
    sender_key: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    from_email: str,
    from_name: str | None,
    use_tls: bool,
    use_ssl: bool,
    is_active: bool,
) -> str:
    normalized_key = sender_key.strip().lower()
    if not _valid_sender_key(normalized_key):
        raise ValueError("Sender key must use only lowercase letters, numbers, '-' or '_'")
    if not host.strip():
        raise ValueError("SMTP host is required")
    if not from_email.strip():
        raise ValueError("From email is required")

    values: dict[str, tuple[SettingValueType, str | None, object | None, bool]] = {
        "host": (SettingValueType.string, host.strip(), None, False),
        "port": (SettingValueType.integer, str(int(port)), None, False),
        "username": (SettingValueType.string, (username or "").strip(), None, False),
        "from_email": (SettingValueType.string, from_email.strip(), None, False),
        "from_name": (SettingValueType.string, (from_name or "DotMac SM").strip() or "DotMac SM", None, False),
        "use_tls": (SettingValueType.boolean, "true" if use_tls else "false", use_tls, False),
        "use_ssl": (SettingValueType.boolean, "true" if use_ssl else "false", use_ssl, False),
        "is_active": (SettingValueType.boolean, "true" if is_active else "false", is_active, False),
    }
    for field, (value_type, value_text, value_json, is_secret) in values.items():
        notification_settings.upsert_by_key(
            db,
            _sender_setting_key(normalized_key, field),
            DomainSettingUpdate(
                value_type=value_type,
                value_text=value_text,
                value_json=value_json,
                is_secret=is_secret,
                is_active=True,
            ),
        )

    if password is not None and password.strip():
        notification_settings.upsert_by_key(
            db,
            _sender_setting_key(normalized_key, "password"),
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=password.strip(),
                is_secret=True,
                is_active=True,
            ),
        )

    return normalized_key


def deactivate_smtp_sender(db: Session, sender_key: str) -> None:
    prefix = f"{SMTP_SENDER_KEY_PREFIX}{sender_key.strip().lower()}."
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key.like(f"{prefix}%"))
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    for setting in settings:
        notification_settings.upsert_by_key(
            db,
            setting.key,
            DomainSettingUpdate(is_active=False),
        )


def _resolve_smtp_sender_config(
    db: Session,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict[str, Any] | None:
    available = {sender["sender_key"]: sender for sender in list_smtp_senders(db) if sender.get("is_active", True)}
    if not available:
        return None

    selected_key = (sender_key or "").strip().lower()
    if not selected_key and activity:
        selected_key = get_smtp_activity_map(db).get(activity.strip(), "")
    if not selected_key:
        selected_key = get_default_smtp_sender_key(db)
    if selected_key not in available:
        selected_key = sorted(available.keys())[0]

    selected = dict(available[selected_key])
    if selected.get("has_password"):
        password = _setting_value(db, _sender_setting_key(selected_key, "password"))
        if password:
            selected["password"] = password
    selected["user"] = selected.get("username")
    selected["from_addr"] = selected.get("from_email")
    selected["sender_key"] = selected_key
    return selected


def _get_smtp_config(
    db: Session | None,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict:
    if db is not None:
        selected = _resolve_smtp_sender_config(db, sender_key=sender_key, activity=activity)
        if selected:
            return selected
    return _legacy_smtp_config(db)


def get_smtp_config(
    db: Session | None,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict:
    """Public accessor for resolved SMTP configuration."""
    return _get_smtp_config(db, sender_key=sender_key, activity=activity)


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
    sender_key: str | None = None,
    activity: str | None = None,
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
    config = _get_smtp_config(db, sender_key=sender_key, activity=activity)

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

        logger.info(
            "Email sent successfully to %s via sender %s",
            to_email,
            config.get("sender_key", "legacy"),
        )
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
        logger.error(
            "Failed to send email to %s via sender %s: %s",
            to_email,
            config.get("sender_key", "legacy"),
            e,
        )
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

    return send_email(
        db,
        to_email,
        subject,
        body_html,
        body_text,
        activity="auth_password_reset",
    )


def send_user_invite_email(
    db: Session,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    next_login_path: str | None = None,
) -> bool:
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
    query = {"token": reset_token}
    if next_login_path and next_login_path.startswith("/"):
        query["next_login"] = next_login_path
    reset_url = f"{app_url}/auth/reset-password?{urlencode(query)}"

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

    return send_email(
        db,
        to_email,
        subject,
        body_html,
        body_text,
        activity="auth_user_invite",
    )
