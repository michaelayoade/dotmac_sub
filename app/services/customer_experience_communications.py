"""Customer-work lifecycle communication intents.

Domain owners request a named outcome here. Channel selection, recipient
selection, preference/suppression policy, delivery state and transport remain
owned by the communications control plane.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel
from app.models.work_order import WorkOrder
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.communication_intents import (
    CommunicationIntent,
    CommunicationIntentResult,
    submit,
)
from app.services.domain_settings import DomainSettings
from app.services.work_order_assignment_contracts import WorkOrderAssignmentPreview
from app.services.work_order_errors import WorkOrderCommandError

DOCUMENT_CHANGE_NOTIFICATION_EVENTS: tuple[str, ...] = (
    "project_status_changed",
    "project_task_status_changed",
    "project_updated",
    "project_task_updated",
    "project_completed",
    "project_task_completed",
    "support_ticket_created_admin",
    "support_ticket_comment_added",
    "support_ticket_status_changed",
    "support_ticket_updated",
    "support_ticket_resolution_confirmation",
    "support_csat_request",
    "work_order_assigned",
    "work_order_reassigned",
    "work_order_schedule_changed",
    "work_order_en_route",
    "work_order_arrived",
    "work_order_complete",
    "work_order_unable_to_complete",
)


def document_change_notification_enabled(
    db: Session,
    event_type: str,
    *,
    default: bool = True,
) -> bool:
    """Return whether a document-change customer notification is enabled.

    Shape is owned by the notification-domain setting
    ``document_change_notification_events_enabled``. Operators may store a JSON
    object such as ``{"default": true, "support_ticket_comment_added": false}``
    or a list/CSV of enabled event names.
    """
    configured = settings_spec.resolve_value(
        db,
        SettingDomain.notification,
        "document_change_notification_events_enabled",
    )
    normalized_event = event_type.strip()
    if isinstance(configured, dict):
        if normalized_event in configured:
            return _truthy(configured[normalized_event], default=default)
        if "*" in configured:
            return _truthy(configured["*"], default=default)
        if "default" in configured:
            return _truthy(configured["default"], default=default)
        return default
    if isinstance(configured, (list, tuple, set)):
        return normalized_event in {str(item).strip() for item in configured}
    if isinstance(configured, str):
        text = configured.strip()
        if not text:
            return default
        if text.lower() in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text.lower() in {"0", "false", "no", "off", "disabled"}:
            return False
        return normalized_event in {item.strip() for item in text.split(",")}
    return default


def document_change_notification_policy(db: Session) -> dict[str, bool]:
    return {
        event_type: document_change_notification_enabled(
            db,
            event_type,
            default=not event_type.endswith("_updated"),
        )
        for event_type in DOCUMENT_CHANGE_NOTIFICATION_EVENTS
    }


def set_document_change_notification_policy(
    db: Session,
    enabled_events: dict[str, bool],
) -> dict[str, bool]:
    payload = {
        event_type: bool(enabled_events.get(event_type, False))
        for event_type in DOCUMENT_CHANGE_NOTIFICATION_EVENTS
    }
    spec = settings_spec.get_spec(
        SettingDomain.notification,
        "document_change_notification_events_enabled",
    )
    if spec is None:
        raise RuntimeError("document change notification setting is not registered")
    DomainSettings(SettingDomain.notification).upsert_by_key(
        db,
        "document_change_notification_events_enabled",
        DomainSettingUpdate(
            value_type=spec.value_type,
            value_text=None,
            value_json=payload,
            is_secret=False,
            is_active=True,
        ),
    )
    db.commit()
    return document_change_notification_policy(db)


def _truthy(value: object, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def request_update(
    db: Session,
    *,
    subscriber_id: UUID,
    event_type: str,
    subject: str,
    body: str,
    metadata: dict[str, object],
    dedupe_key: str,
    default_channels: Iterable[NotificationChannel],
) -> None:
    if not document_change_notification_enabled(db, event_type):
        return
    submit(
        db,
        CommunicationIntent(
            subscriber_id=subscriber_id,
            event_type=event_type,
            category="service",
            template_code=event_type,
            subject=subject,
            body=body,
            default_channels=tuple(default_channels),
            include_reseller=False,
            persist_policy_suppressions=True,
            metadata={"customer_experience": True, **metadata},
            dedupe_key=dedupe_key,
        ),
    )


def request_field_event(
    db: Session,
    *,
    work_order: WorkOrder,
    event: str,
    field_event_id: UUID,
) -> None:
    messages = {
        "en_route": (
            "Your technician is on the way",
            f"The technician for {work_order.title} is on the way.",
        ),
        "arrived": (
            "Your technician has arrived",
            f"The technician for {work_order.title} has arrived at the site.",
        ),
        "complete": (
            "Technician visit completed",
            f"Field work for {work_order.title} is complete. You can review the visit in self-care.",
        ),
        "unable_to_complete": (
            "Technician visit needs follow-up",
            f"Field work for {work_order.title} could not be completed. The responsible team will review the outcome.",
        ),
    }
    message = messages.get(event)
    if message is None:
        return
    # Shared infrastructure work has no individual customer recipient.
    if work_order.subscriber_id is None:
        return
    terminal = event in {"complete", "unable_to_complete"}
    request_update(
        db,
        subscriber_id=work_order.subscriber_id,
        event_type=f"work_order_{event}",
        subject=message[0],
        body=message[1],
        metadata={
            "type": "work_order",
            "work_order_id": work_order.public_id,
            "work_order_pk": str(work_order.id),
            "project_id": str(work_order.project_id) if work_order.project_id else None,
            "project_task_id": str(work_order.project_task_id)
            if work_order.project_task_id
            else None,
            "ticket_id": str(work_order.origin_ticket_id)
            if work_order.origin_ticket_id
            else None,
            "field_event_id": str(field_event_id),
        },
        dedupe_key=f"field-event:{field_event_id}",
        default_channels=(
            (
                NotificationChannel.email,
                NotificationChannel.whatsapp,
                NotificationChannel.push,
            )
            if terminal
            else (NotificationChannel.whatsapp, NotificationChannel.push)
        ),
    )


class AssignmentCommunicationEvent(StrEnum):
    assigned = "work_order_assigned"
    reassigned = "work_order_reassigned"
    schedule_changed = "work_order_schedule_changed"


@dataclass(frozen=True, kw_only=True)
class AssignmentCommunicationRequest:
    subscriber_id: UUID | None
    work_order_id: UUID
    work_order_public_id: str
    title: str
    command_id: UUID
    queue_id: UUID
    preview: WorkOrderAssignmentPreview
    project_id: UUID | None = None
    project_task_id: UUID | None = None
    ticket_id: UUID | None = None


def request_assignment_update(
    db: Session, *, request: AssignmentCommunicationRequest
) -> CommunicationIntentResult | None:
    """Stage a customer intent inside the authoritative assignment transaction."""
    if not db.in_transaction():
        raise WorkOrderCommandError(
            "assignment_communication_transaction_required",
            "Assignment communication requires its authoritative transaction",
        )
    if request.preview.work_order_id != request.work_order_public_id:
        raise WorkOrderCommandError(
            "assignment_communication_scope_mismatch",
            "Assignment preview does not match the communication scope",
        )
    if request.subscriber_id is None:
        return None
    previous, result = request.preview.previous, request.preview.result
    previous_target = (
        previous.technician_id,
        previous.vendor_id,
        previous.vendor_user_id,
    )
    result_target = (result.technician_id, result.vendor_id, result.vendor_user_id)
    if previous_target != result_target:
        event = (
            AssignmentCommunicationEvent.assigned
            if previous_target == (None, None, None)
            else AssignmentCommunicationEvent.reassigned
        )
    elif (previous.scheduled_start, previous.scheduled_end) != (
        result.scheduled_start,
        result.scheduled_end,
    ):
        event = AssignmentCommunicationEvent.schedule_changed
    else:
        return None
    if not document_change_notification_enabled(db, event.value):
        return None
    name = result.technician_name or result.vendor_name or "Your field team"
    schedule = (
        f" Scheduled start: {result.scheduled_start.isoformat()}."
        if result.scheduled_start is not None
        else " The visit schedule will be confirmed."
    )
    subject = {
        AssignmentCommunicationEvent.assigned: "Your technician visit is assigned",
        AssignmentCommunicationEvent.reassigned: "Your technician visit assignment changed",
        AssignmentCommunicationEvent.schedule_changed: "Your technician visit schedule changed",
    }[event]
    return submit(
        db,
        CommunicationIntent(
            subscriber_id=request.subscriber_id,
            event_type=event.value,
            category="service",
            template_code=event.value,
            subject=subject,
            body=f"{name} will handle {request.title}.{schedule}",
            default_channels=(
                NotificationChannel.email,
                NotificationChannel.whatsapp,
                NotificationChannel.push,
            ),
            include_reseller=False,
            persist_policy_suppressions=True,
            metadata={
                "customer_experience": True,
                "type": "work_order",
                "work_order_id": request.work_order_public_id,
                "work_order_pk": str(request.work_order_id),
                "assignment_command_id": str(request.command_id),
                "assignment_queue_id": str(request.queue_id),
                "technician_id": str(result.technician_id)
                if result.technician_id
                else None,
                "vendor_id": str(result.vendor_id) if result.vendor_id else None,
                "vendor_user_id": str(result.vendor_user_id)
                if result.vendor_user_id
                else None,
                "technician_name": result.technician_name,
                "vendor_name": result.vendor_name,
                "scheduled_start": result.scheduled_start.isoformat()
                if result.scheduled_start
                else None,
                "scheduled_end": result.scheduled_end.isoformat()
                if result.scheduled_end
                else None,
                "project_id": str(request.project_id) if request.project_id else None,
                "project_task_id": str(request.project_task_id)
                if request.project_task_id
                else None,
                "ticket_id": str(request.ticket_id) if request.ticket_id else None,
            },
            dedupe_key=f"work-order-assignment:{request.command_id}",
        ),
    )
