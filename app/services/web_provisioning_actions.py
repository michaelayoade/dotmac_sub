"""Service helpers for admin provisioning action routes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel, NotificationStatus
from app.models.provisioning import (
    AppointmentStatus,
    ProvisioningStepType,
    ProvisioningVendor,
    ServiceOrderStatus,
    TaskStatus,
)
from app.schemas.notification import NotificationCreate
from app.schemas.provisioning import (
    InstallAppointmentCreate,
    InstallAppointmentUpdate,
    ProvisioningStepCreate,
    ProvisioningStepUpdate,
    ProvisioningTaskCreate,
    ProvisioningTaskUpdate,
    ProvisioningWorkflowCreate,
    ProvisioningWorkflowUpdate,
    ServiceOrderUpdate,
)
from app.services import notification as notification_service
from app.services import provisioning as provisioning_service
from app.services import subscriber as subscriber_service
from app.services import web_admin as web_admin_service
from app.services.audit_helpers import diff_dicts, log_audit_event, model_to_dict
from app.validators.forms import parse_datetime

MENTION_EMAIL_RE = re.compile(r"@([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _actor_id(request) -> str | None:
    return web_admin_service.get_actor_id(request)


def _extract_mentioned_emails(comment: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in MENTION_EMAIL_RE.findall(comment):
        email = match.strip().lower()
        if email and email not in seen:
            seen.add(email)
            ordered.append(email)
    return ordered


def _notify_tagged_users(
    db: Session,
    request,
    *,
    order_id: UUID,
    comment: str,
    mentioned_emails: list[str],
) -> int:
    if not mentioned_emails:
        return 0

    actor_subscriber_id = _actor_id(request)
    recipients = subscriber_service.subscribers.list_active_by_emails(
        db, mentioned_emails
    )

    notified = 0
    base_url = str(request.base_url).rstrip("/")
    order_url = f"{base_url}/admin/provisioning/orders/{order_id}"
    short_order_id = str(order_id)[:8]
    subject = f"You were mentioned in Service Order {short_order_id}"
    body = (
        f"You were tagged in a comment on Service Order {short_order_id}.\n\n"
        f"Comment:\n{comment}\n\n"
        f"Open order: {order_url}"
    )

    for subscriber in recipients:
        if actor_subscriber_id and str(subscriber.id) == actor_subscriber_id:
            continue

        notification_service.notifications.create(
            db,
            NotificationCreate(
                channel=NotificationChannel.push,
                recipient=str(subscriber.id),
                subject=subject,
                body=body,
                status=NotificationStatus.delivered,
                sent_at=datetime.now(UTC),
            ),
        )
        notification_service.notifications.create(
            db,
            NotificationCreate(
                channel=NotificationChannel.email,
                recipient=subscriber.email,
                subject=subject,
                body=body,
                status=NotificationStatus.queued,
            ),
        )
        notified += 1
    return notified


def add_order_comment_with_mentions(
    db: Session,
    request,
    *,
    order_id: UUID,
    comment: str,
) -> bool:
    """Add an audited service-order comment and notify mentioned users."""
    order = provisioning_service.service_orders.get(db, str(order_id))
    if not order:
        return False

    cleaned_comment = comment.strip()
    if not cleaned_comment:
        return True

    mentions = _extract_mentioned_emails(cleaned_comment)
    notified_count = _notify_tagged_users(
        db,
        request,
        order_id=order_id,
        comment=cleaned_comment,
        mentioned_emails=mentions,
    )

    log_audit_event(
        db=db,
        request=request,
        action="comment",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "comment": cleaned_comment,
            "mentions": mentions,
            "notified_users": notified_count,
        },
    )
    return True


def update_order_status_with_audit(
    db: Session,
    request,
    *,
    order_id: UUID,
    new_status: str,
) -> None:
    before = provisioning_service.service_orders.get(db, str(order_id))
    before_state = model_to_dict(before) if before else None
    payload = ServiceOrderUpdate(status=ServiceOrderStatus(new_status))
    provisioning_service.service_orders.update(db, str(order_id), payload)
    after = provisioning_service.service_orders.get(db, str(order_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )


def add_appointment_with_audit(
    db: Session,
    request,
    *,
    order_id: UUID,
    scheduled_start: str,
    scheduled_end: str,
    technician: str | None,
    notes: str | None,
    is_self_install: bool,
) -> bool:
    start = parse_datetime(scheduled_start)
    end = parse_datetime(scheduled_end)
    if not start or not end:
        return False

    payload = InstallAppointmentCreate(
        service_order_id=order_id,
        scheduled_start=start,
        scheduled_end=end,
        technician=technician or None,
        notes=notes or None,
        is_self_install=is_self_install,
    )
    appointment = provisioning_service.install_appointments.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "appointment_id": str(appointment.id),
            "technician": technician or None,
            "scheduled_start": start.isoformat(),
            "scheduled_end": end.isoformat(),
        },
    )
    return True


def add_task_with_audit(
    db: Session,
    request,
    *,
    order_id: UUID,
    name: str,
    assigned_to: str | None,
    notes: str | None,
) -> None:
    payload = ProvisioningTaskCreate(
        service_order_id=order_id,
        name=name,
        assigned_to=assigned_to or None,
        notes=notes or None,
    )
    task = provisioning_service.provisioning_tasks.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={
            "task_id": str(task.id),
            "name": name,
            "assigned_to": assigned_to or None,
        },
    )


def update_task_status_with_audit(
    db: Session,
    request,
    *,
    order_id: UUID,
    task_id: UUID,
    new_status: str,
) -> None:
    before = provisioning_service.provisioning_tasks.get(db, str(task_id))
    before_state = model_to_dict(before) if before else None
    payload = ProvisioningTaskUpdate(status=TaskStatus(new_status))
    provisioning_service.provisioning_tasks.update(db, str(task_id), payload)
    after = provisioning_service.provisioning_tasks.get(db, str(task_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )


def run_order_workflow_with_audit(
    db: Session,
    request,
    *,
    order_id: UUID,
    workflow_id: str,
) -> None:
    provisioning_service.service_orders.run_for_order(db, str(order_id), workflow_id)
    log_audit_event(
        db=db,
        request=request,
        action="run_workflow",
        entity_type="service_order",
        entity_id=str(order_id),
        actor_id=_actor_id(request),
        metadata={"workflow_id": workflow_id},
    )


def create_workflow_with_audit(
    db: Session,
    request,
    *,
    name: str,
    vendor: str,
    description: str | None,
    is_active: bool,
):
    payload = ProvisioningWorkflowCreate(
        name=name,
        vendor=ProvisioningVendor(vendor),
        description=description or None,
        is_active=is_active,
    )
    workflow = provisioning_service.provisioning_workflows.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="provisioning_workflow",
        entity_id=str(workflow.id),
        actor_id=_actor_id(request),
        metadata={"name": name, "vendor": vendor},
    )
    return workflow


def update_workflow_with_audit(
    db: Session,
    request,
    *,
    workflow_id: UUID,
    name: str,
    vendor: str,
    description: str | None,
    is_active: bool,
) -> None:
    before = provisioning_service.provisioning_workflows.get(db, str(workflow_id))
    before_state = model_to_dict(before) if before else None
    payload = ProvisioningWorkflowUpdate(
        name=name,
        vendor=ProvisioningVendor(vendor),
        description=description or None,
        is_active=is_active,
    )
    provisioning_service.provisioning_workflows.update(db, str(workflow_id), payload)
    after = provisioning_service.provisioning_workflows.get(db, str(workflow_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )


def _parse_step_config(config_json: str | None) -> dict | None:
    if not config_json:
        return None
    try:
        parsed = json.loads(config_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def create_step_with_audit(
    db: Session,
    request,
    *,
    workflow_id: UUID,
    name: str,
    step_type: str,
    order_index: int,
    config_json: str | None,
):
    payload = ProvisioningStepCreate(
        workflow_id=workflow_id,
        name=name,
        step_type=ProvisioningStepType(step_type),
        order_index=order_index,
        config=_parse_step_config(config_json),
    )
    step = provisioning_service.provisioning_steps.create(db, payload)
    log_audit_event(
        db=db,
        request=request,
        action="create_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata={
            "step_id": str(step.id),
            "name": name,
            "step_type": step_type,
            "order_index": order_index,
        },
    )
    return step


def update_step_with_audit(
    db: Session,
    request,
    *,
    workflow_id: UUID,
    step_id: UUID,
    name: str | None,
    step_type: str | None,
    order_index: int | None,
    config_json: str | None,
) -> None:
    before = provisioning_service.provisioning_steps.get(db, str(step_id))
    before_state = model_to_dict(before) if before else None
    update_data: dict[str, object] = {}
    if name:
        update_data["name"] = name
    if step_type:
        update_data["step_type"] = ProvisioningStepType(step_type)
    if order_index is not None:
        update_data["order_index"] = order_index
    config = _parse_step_config(config_json)
    if config is not None:
        update_data["config"] = config

    payload = ProvisioningStepUpdate.model_validate(update_data)
    provisioning_service.provisioning_steps.update(db, str(step_id), payload)
    after = provisioning_service.provisioning_steps.get(db, str(step_id))
    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata=metadata,
    )


def delete_step_with_audit(
    db: Session,
    request,
    *,
    workflow_id: UUID,
    step_id: UUID,
) -> None:
    step = provisioning_service.provisioning_steps.get(db, str(step_id))
    payload = ProvisioningStepUpdate(is_active=False)
    provisioning_service.provisioning_steps.update(db, str(step_id), payload)
    log_audit_event(
        db=db,
        request=request,
        action="delete_step",
        entity_type="provisioning_workflow",
        entity_id=str(workflow_id),
        actor_id=_actor_id(request),
        metadata={"step_id": str(step_id), "name": getattr(step, "name", None)},
    )


def update_appointment_status_with_audit(
    db: Session,
    request,
    *,
    appointment_id: UUID,
    new_status: str,
) -> None:
    before = provisioning_service.install_appointments.get(db, str(appointment_id))
    before_state = model_to_dict(before) if before else None
    payload = InstallAppointmentUpdate(status=AppointmentStatus(new_status))
    provisioning_service.install_appointments.update(db, str(appointment_id), payload)
    after = provisioning_service.install_appointments.get(db, str(appointment_id))
    order_id = str(
        (after and getattr(after, "service_order_id", None))
        or (before and getattr(before, "service_order_id", None))
        or ""
    )
    if not order_id:
        return

    metadata = None
    if before_state is not None and after:
        changes = diff_dicts(before_state, model_to_dict(after))
        metadata = {"changes": changes} if changes else None
    log_audit_event(
        db=db,
        request=request,
        action="update_appointment",
        entity_type="service_order",
        entity_id=order_id,
        actor_id=_actor_id(request),
        metadata=metadata,
    )
