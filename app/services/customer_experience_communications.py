"""Customer-work lifecycle communication intents.

Domain owners request a named outcome here. Channel selection, recipient
selection, preference/suppression policy, delivery state and transport remain
owned by the communications control plane.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.models.work_order import WorkOrder
from app.services.communication_intents import CommunicationIntent, submit


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
