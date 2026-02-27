"""SMS service with multi-provider support.

Supports:
- Twilio
- Africa's Talking
- Generic HTTP webhook

Configuration via environment variables or DomainSettings:
- SMS_PROVIDER: twilio | africastalking | webhook
- SMS_API_KEY, SMS_API_SECRET
- SMS_FROM_NUMBER
- SMS_WEBHOOK_URL (for webhook provider)
"""

import ipaddress
import logging
import os
import socket
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
    NotificationTemplate,
)

logger = logging.getLogger(__name__)


def _validate_webhook_target(webhook_url: str) -> None:
    """Reject private/internal webhook targets to prevent SSRF."""
    parsed_url = urllib.parse.urlparse(webhook_url)
    hostname = parsed_url.hostname
    if not hostname:
        raise ValueError("SMS webhook URL is missing hostname")

    resolved_ips: list[str] = []
    try:
        resolved_ips.append(str(ipaddress.ip_address(hostname)))
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise ValueError(
                f"SMS webhook hostname resolution failed: {hostname}"
            ) from exc
        resolved_ips.extend(item[4][0] for item in resolved)

    for ip in resolved_ips:
        resolved_ip = ipaddress.ip_address(ip)
        if (
            resolved_ip.is_private
            or resolved_ip.is_loopback
            or resolved_ip.is_link_local
        ):
            raise ValueError("SSRF blocked")


def _get_setting(db: Session, key: str, env_key: str | None = None, default: str | None = None) -> str | None:
    """Get setting from environment or database."""
    if env_key:
        env_value = os.getenv(env_key)
        if env_value:
            return env_value

    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if setting and setting.value_text:
        return setting.value_text
    return default


def _normalize_phone(phone: str) -> str:
    """Normalize phone number to E.164 format."""
    # Remove common formatting characters
    normalized = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    # Ensure it starts with +
    if not normalized.startswith("+"):
        # Assume Nigerian number if no country code
        if normalized.startswith("0"):
            normalized = "+234" + normalized[1:]
        else:
            normalized = "+" + normalized

    return normalized


def _send_via_twilio(
    api_key: str,
    api_secret: str,
    from_number: str,
    to_phone: str,
    body: str,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via Twilio.

    Returns: (success, message_sid, error_message)
    """
    try:
        # Twilio uses account_sid as api_key and auth_token as api_secret
        url = f"https://api.twilio.com/2010-04-01/Accounts/{api_key}/Messages.json"

        response = httpx.post(
            url,
            auth=(api_key, api_secret),
            data={
                "From": from_number,
                "To": to_phone,
                "Body": body,
            },
            timeout=30.0,
        )

        if response.status_code in (200, 201):
            data = response.json()
            return True, data.get("sid"), None
        else:
            error_data = response.json() if response.content else {}
            error_msg = error_data.get("message", f"HTTP {response.status_code}")
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=twilio status=%s message=%s",
                    response.status_code,
                    error_msg,
                )
            return False, None, error_msg

    except Exception as exc:
        logger.exception("Twilio SMS failed")
        return False, None, str(exc)


def _send_via_africastalking(
    api_key: str,
    username: str,
    from_number: str | None,
    to_phone: str,
    body: str,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via Africa's Talking.

    Returns: (success, message_id, error_message)
    """
    try:
        url = "https://api.africastalking.com/version1/messaging"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": api_key,
        }

        data: dict[str, Any] = {
            "username": username,
            "to": to_phone,
            "message": body,
        }
        if from_number:
            data["from"] = from_number

        response = httpx.post(url, headers=headers, data=data, timeout=30.0)

        if response.status_code in (200, 201):
            resp_data = response.json()
            sms_data = resp_data.get("SMSMessageData", {})
            recipients = sms_data.get("Recipients", [])
            if recipients:
                recipient = recipients[0]
                status = recipient.get("status", "")
                if status in ("Success", "Sent"):
                    return True, recipient.get("messageId"), None
                else:
                    return False, None, status
            return False, None, "No recipients in response"
        else:
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=africastalking status=%s body=%s",
                    response.status_code,
                    response.text,
                )
            return False, None, f"HTTP {response.status_code}"

    except Exception as exc:
        logger.exception("Africa's Talking SMS failed")
        return False, None, str(exc)


def _send_via_webhook(
    webhook_url: str,
    api_key: str | None,
    to_phone: str,
    body: str,
) -> tuple[bool, str | None, str | None]:
    """Send SMS via generic HTTP webhook.

    Returns: (success, external_id, error_message)
    """
    try:
        _validate_webhook_target(webhook_url)

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "to": to_phone,
            "message": body,
        }

        response = httpx.post(webhook_url, headers=headers, json=payload, timeout=30.0)

        if response.status_code in (200, 201, 202):
            try:
                data = response.json()
                external_id = data.get("message_id") or data.get("id") or data.get("sid")
                return True, external_id, None
            except Exception:
                return True, None, None
        else:
            if response.status_code in (401, 403):
                logger.error(
                    "sms_auth_failed provider=webhook status=%s body=%s",
                    response.status_code,
                    response.text,
                )
            return False, None, f"HTTP {response.status_code}"

    except Exception as exc:
        logger.exception("Webhook SMS failed")
        return False, None, str(exc)


def send_sms(
    db: Session,
    to_phone: str,
    body: str,
    track: bool = True,
) -> bool:
    """Send an SMS message.

    Args:
        db: Database session
        to_phone: Recipient phone number
        body: Message content
        track: Whether to create notification/delivery records

    Returns:
        True if SMS was sent successfully
    """
    provider = _get_setting(db, "sms_provider", "SMS_PROVIDER", "webhook")
    api_key = _get_setting(db, "sms_api_key", "SMS_API_KEY")
    api_secret = _get_setting(db, "sms_api_secret", "SMS_API_SECRET")
    from_number = _get_setting(db, "sms_from_number", "SMS_FROM_NUMBER")
    webhook_url = _get_setting(db, "sms_webhook_url", "SMS_WEBHOOK_URL")

    normalized_phone = _normalize_phone(to_phone)

    # Create notification record if tracking
    notification = None
    if track:
        notification = Notification(
            channel=NotificationChannel.sms,
            recipient=normalized_phone,
            body=body,
            status=NotificationStatus.sending,
        )
        db.add(notification)
        db.flush()

    # Send based on provider
    success = False
    external_id = None
    error_message = None

    if provider == "twilio":
        if not api_key or not api_secret or not from_number:
            error_message = "Twilio configuration incomplete"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_twilio(
                api_key, api_secret, from_number, normalized_phone, body
            )

    elif provider == "africastalking":
        # Africa's Talking requires a username; default to "sandbox" if unset.
        username = _get_setting(db, "sms_username", "SMS_USERNAME", "sandbox") or "sandbox"
        if not api_key:
            error_message = "Africa's Talking API key not configured"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_africastalking(
                api_key, username, from_number, normalized_phone, body
            )

    elif provider == "webhook":
        if not webhook_url:
            error_message = "SMS webhook URL not configured"
            logger.error(error_message)
        else:
            success, external_id, error_message = _send_via_webhook(
                webhook_url, api_key, normalized_phone, body
            )

    else:
        error_message = f"Unknown SMS provider: {provider}"
        logger.error(error_message)

    # Update notification status
    if notification:
        notification.status = NotificationStatus.delivered if success else NotificationStatus.failed
        notification.sent_at = datetime.now(UTC) if success else None

        # Create delivery record
        delivery = NotificationDelivery(
            notification_id=notification.id,
            channel=NotificationChannel.sms,
            recipient=normalized_phone,
            status=DeliveryStatus.delivered if success else DeliveryStatus.failed,
            occurred_at=datetime.now(UTC),
            external_id=external_id,
            error_message=error_message,
        )
        db.add(delivery)
        db.commit()

    if success:
        logger.info(f"SMS sent to {normalized_phone}")
    else:
        logger.error(f"SMS failed to {normalized_phone}: {error_message}")

    return success


def send_with_template(
    db: Session,
    template_code: str,
    to_phone: str,
    context: dict[str, Any],
) -> bool:
    """Send SMS using a notification template.

    Args:
        db: Database session
        template_code: The template's code identifier
        to_phone: Recipient phone number
        context: Template variables for substitution

    Returns:
        True if SMS was sent successfully
    """
    template = (
        db.query(NotificationTemplate)
        .filter(NotificationTemplate.code == template_code)
        .filter(NotificationTemplate.channel == NotificationChannel.sms)
        .filter(NotificationTemplate.is_active.is_(True))
        .first()
    )

    if not template:
        logger.error(f"SMS template not found: {template_code}")
        return False

    # Simple template substitution using Python format strings
    try:
        body = template.body
        for key, value in context.items():
            body = body.replace(f"{{{{{key}}}}}", str(value))
            body = body.replace(f"{{{{ {key} }}}}", str(value))
            body = body.replace(f"${{{key}}}", str(value))
    except Exception as exc:
        logger.error(f"Template substitution failed: {exc}")
        body = template.body

    return send_sms(db, to_phone, body, track=True)
