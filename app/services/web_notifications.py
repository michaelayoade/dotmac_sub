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
from app.services import notification as notification_service
from app.services import notification_template_renderer as template_renderer


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
    payload = NotificationTemplateCreate(
        name=name.strip(),
        code=_normalize_template_code(code),
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
    payload = NotificationTemplateUpdate(
        name=name.strip(),
        code=_normalize_template_code(code),
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
        result = whatsapp_service.send_text_message(
            db=db,
            recipient=recipient,
            body=rendered_body,
            dry_run=False,
        )
        if not result.get("ok"):
            raise RuntimeError(
                str(result.get("response") or "Failed to send WhatsApp test")
            )
        return f"Test WhatsApp message sent to {test_recipient}"

    return f"Test notification queued for {template.channel.value}"


def queue_context(
    db: Session,
    *,
    status: str | None,
    channel: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    effective_status = status if status else "queued"
    effective_channel = channel if channel else None
    notifications_list = notification_service.notifications.list(
        db=db,
        channel=effective_channel,
        status=effective_status,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    total = notification_service.notifications.count(
        db=db,
        channel=effective_channel,
        status=effective_status,
    )
    total_pages = (total + per_page - 1) // per_page if total else 1
    return {
        "notifications": notifications_list,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "status": effective_status,
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
