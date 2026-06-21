"""Service helpers for admin notification web routes."""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationStatus,
)
from app.schemas.notification import (
    NotificationTemplateCreate,
    NotificationTemplateUpdate,
)
from app.services import email as email_service
from app.services import notification as notification_service
from app.services import notification_template_renderer as template_renderer
from app.services import sms as sms_service
from app.services.integrations.connectors import whatsapp as whatsapp_connector


def channels() -> list[str]:
    return [item.value for item in NotificationChannel]


def notification_statuses() -> list[str]:
    return [item.value for item in NotificationStatus]


def delivery_statuses() -> list[str]:
    return [item.value for item in DeliveryStatus]


def templates_list_context(
    db: Session,
    *,
    channel: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    effective_channel = channel if channel else None
    template_list = notification_service.templates.list(
        db=db,
        channel=effective_channel,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=per_page,
        offset=offset,
    )
    total = notification_service.templates.count(db=db, channel=effective_channel)
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "templates": template_list,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "channel": channel,
        "channel_counts": notification_service.templates.channel_counts(db),
        "channels": channels(),
    }


def template_form_context(
    db: Session,
    *,
    template_id: UUID | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    template = None
    if template_id is not None:
        template = notification_service.templates.get(
            db=db, template_id=str(template_id)
        )
        if not template:
            return None

    is_edit = template_id is not None
    context: dict[str, object] = {
        "channels": channels(),
        "action_url": f"/admin/notifications/templates/{template_id}"
        if is_edit
        else "/admin/notifications/templates",
        "form_title": "Edit Notification Template"
        if is_edit
        else "New Notification Template",
        "submit_label": "Update Template" if is_edit else "Create Template",
        "template_variables": template_renderer.TEMPLATE_VARIABLES,
    }
    if template is not None:
        context["template"] = template
    if error:
        context["error"] = error
    return context


def _normalize_template_code(code: str) -> str:
    return code.strip().lower().replace(" ", "_")


def create_template(
    db: Session,
    *,
    name: str,
    code: str,
    channel: str,
    subject: str | None,
    body: str,
):
    normalized_code = _normalize_template_code(code)
    template_renderer.validate_template_text(subject, body, code=normalized_code)
    payload = NotificationTemplateCreate(
        name=name.strip(),
        code=normalized_code,
        channel=NotificationChannel(channel),
        subject=subject.strip() if subject else None,
        body=body.strip(),
    )
    return notification_service.templates.create(db=db, payload=payload)


def update_template(
    db: Session,
    *,
    template_id: UUID,
    name: str,
    code: str,
    channel: str,
    subject: str | None,
    body: str,
    is_active: bool,
):
    normalized_code = _normalize_template_code(code)
    template_renderer.validate_template_text(subject, body, code=normalized_code)
    payload = NotificationTemplateUpdate(
        name=name.strip(),
        code=normalized_code,
        channel=NotificationChannel(channel),
        subject=subject.strip() if subject else None,
        body=body.strip(),
        is_active=is_active,
    )
    return notification_service.templates.update(
        db=db, template_id=str(template_id), payload=payload
    )


def delete_template(db: Session, *, template_id: UUID) -> None:
    notification_service.templates.delete(db=db, template_id=str(template_id))


def preview_variables(test_variables_json: str | None) -> dict[str, str]:
    variables = template_renderer.default_preview_variables()
    if not test_variables_json or not test_variables_json.strip():
        return variables

    parsed = json.loads(test_variables_json)
    if not isinstance(parsed, dict):
        raise ValueError("test_variables_json must be a JSON object")
    for key, value in parsed.items():
        variables[str(key)] = "" if value is None else str(value)
    return variables


def render_template_preview(
    db: Session,
    *,
    template_id: UUID,
    test_variables_json: str | None,
) -> dict[str, object]:
    template = notification_service.templates.get(db=db, template_id=str(template_id))
    variables = preview_variables(test_variables_json)
    return {
        "rendered_subject": template_renderer.render_template_text(
            template.subject or "",
            variables,
        ),
        "rendered_body": template_renderer.render_template_text(
            template.body, variables
        ),
        "variables": variables,
        "channel": template.channel.value,
    }


def send_template_test(
    db: Session,
    *,
    template_id: UUID,
    test_recipient: str,
    test_variables_json: str | None,
) -> str:
    from app.services import email as email_service
    from app.services import sms as sms_service
    from app.services.integrations.connectors import whatsapp as whatsapp_service

    template = notification_service.templates.get(db=db, template_id=str(template_id))
    variables = preview_variables(test_variables_json)
    rendered_subject = template_renderer.render_template_text(
        template.subject or "Test Notification",
        variables,
    )
    rendered_body = template_renderer.render_template_text(template.body, variables)
    recipient = test_recipient.strip()

    if template.channel == NotificationChannel.sms:
        sms_service.send_sms(db=db, to_phone=recipient, body=rendered_body, track=True)
        return f"Test SMS sent to {test_recipient}"
    if template.channel == NotificationChannel.email:
        email_service.send_email(
            db=db,
            to_email=recipient,
            subject=rendered_subject,
            body_html=rendered_body,
            activity="notification_test",
        )
        return f"Test email sent to {test_recipient}"
    if template.channel == NotificationChannel.whatsapp:
        result = whatsapp_service.send_template_message(
            db=db,
            recipient=recipient,
            template_name=template.code,
            dry_run=False,
        )
        if not result.get("ok"):
            raise RuntimeError(
                str(result.get("response") or "Failed to send WhatsApp test")
            )
        return f"Test WhatsApp message sent to {test_recipient}"

    return f"Test notification queued for {template.channel.value}"


def _email_channel_ready(db: Session) -> tuple[bool, str]:
    senders = email_service.list_smtp_senders(db)
    if senders:
        return True, "SMTP sender profiles configured"
    return False, "No SMTP sender profile configured"


def _sms_channel_ready(db: Session) -> tuple[bool, str]:
    enabled = (
        sms_service._get_setting(db, "sms_enabled", "SMS_ENABLED", "true") or "true"
    )
    if enabled.strip().lower() in {"false", "0", "no", "disabled"}:
        return False, "SMS is disabled"

    provider = (
        sms_service._get_setting(db, "sms_provider", "SMS_PROVIDER", "webhook")
        or "webhook"
    )
    if provider == "twilio":
        account_sid = sms_service._get_setting(db, "sms_api_key", "SMS_API_KEY")
        auth_token = sms_service._get_setting(db, "sms_api_secret", "SMS_API_SECRET")
        from_number = sms_service._get_setting(db, "sms_from_number", "SMS_FROM_NUMBER")
        if account_sid and auth_token and from_number:
            return True, "Twilio credentials configured"
        return False, "Twilio credentials are incomplete"
    if provider == "africastalking":
        api_key = sms_service._get_setting(db, "sms_api_key", "SMS_API_KEY")
        if api_key:
            return True, "Africa's Talking API key configured"
        return False, "Africa's Talking API key is missing"
    if provider == "webhook":
        webhook_url = sms_service._get_setting(db, "sms_webhook_url", "SMS_WEBHOOK_URL")
        if webhook_url:
            return True, "Webhook endpoint configured"
        return False, "Webhook URL is missing"
    return False, "SMS provider is not recognized"


def _whatsapp_channel_ready(db: Session) -> tuple[bool, str]:
    config = whatsapp_connector.load_whatsapp_config(db)
    if not str(config.get("api_key") or "").strip():
        return False, "WhatsApp API key is missing"
    if not str(config.get("phone_number") or "").strip():
        return False, "WhatsApp phone number is missing"
    provider = str(config.get("provider") or "").strip() or "provider"
    return True, f"{provider.replace('_', ' ').title()} is configured"


def bulk_notification_setup_context(db: Session) -> dict[str, object]:
    template_list = notification_service.templates.list(
        db=db,
        channel=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    whatsapp_config = whatsapp_connector.load_whatsapp_config(db)
    whatsapp_registry_templates = [
        {
            "id": (
                f"whatsapp:{str(item.get('name') or '').strip()}:"
                f"{str(item.get('language') or '').strip() or 'en'}"
            ),
            "name": str(item.get("name") or "").strip(),
            "code": str(item.get("name") or "").strip(),
            "language": str(item.get("language") or "").strip() or "en",
            "label": (
                f"{str(item.get('name') or '').strip()} "
                f"({str(item.get('language') or '').strip() or 'en'})"
            ),
            "channel": NotificationChannel.whatsapp.value,
            "subject": "",
            "is_active": True,
            "is_registry_template": True,
        }
        for item in whatsapp_config.get("templates", [])
        if str(item.get("name") or "").strip()
    ]
    channel_checks = {
        NotificationChannel.email.value: _email_channel_ready(db),
        NotificationChannel.sms.value: _sms_channel_ready(db),
        NotificationChannel.whatsapp.value: _whatsapp_channel_ready(db),
    }
    channels_state = [
        {
            "id": channel.value,
            "label": "WhatsApp"
            if channel == NotificationChannel.whatsapp
            else channel.value.capitalize(),
            "enabled": channel.value
            in {
                NotificationChannel.email.value,
                NotificationChannel.sms.value,
                NotificationChannel.whatsapp.value,
            },
            "ready": channel_checks.get(channel.value, (False, "Unsupported"))[0],
            "message": channel_checks.get(channel.value, (False, "Unsupported"))[1],
            "template_count": sum(
                1 for template in template_list if template.channel == channel
            )
            if channel != NotificationChannel.whatsapp
            else len(whatsapp_registry_templates),
            "settings_url": (
                "/admin/system/email"
                if channel == NotificationChannel.email
                else "/admin/integrations/whatsapp/config"
                if channel == NotificationChannel.whatsapp
                else None
            ),
        }
        for channel in (
            NotificationChannel.email,
            NotificationChannel.sms,
            NotificationChannel.whatsapp,
        )
    ]
    templates_state = [
        {
            "id": str(template.id),
            "name": template.name,
            "code": template.code,
            "language": "",
            "label": template.name,
            "channel": template.channel.value,
            "subject": template.subject or "",
            "is_active": bool(template.is_active),
            "is_registry_template": False,
        }
        for template in template_list
        if template.channel != NotificationChannel.whatsapp
    ]
    templates_state.extend(whatsapp_registry_templates)
    return {
        "bulk_notification_channels": channels_state,
        "bulk_notification_templates": templates_state,
    }


def queue_context(
    db: Session,
    *,
    status: str | None,
    channel: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    notification_status = status if status else "queued"
    effective_channel = channel if channel else None
    notifications_list = notification_service.notifications.list(
        db=db,
        channel=effective_channel,
        status=notification_status,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    total = notification_service.notifications.count(
        db=db,
        channel=effective_channel,
        status=notification_status,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "notifications": notifications_list,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": notification_status,
        "channel": channel,
        "status_counts": notification_service.notifications.status_counts(db),
        "channels": channels(),
        "statuses": notification_statuses(),
    }


def history_context(
    db: Session,
    *,
    status: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    deliveries = notification_service.deliveries.list(
        db=db,
        notification_id=None,
        status=status if status else None,
        is_active=True,
        order_by="occurred_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    total = notification_service.deliveries.count(
        db=db,
        status=status if status else None,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "deliveries": deliveries,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": status,
        "statuses": delivery_statuses(),
    }
