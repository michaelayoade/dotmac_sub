"""Service helpers for notification alert-policy and on-call web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from app.models.network_monitoring import AlertSeverity
from app.models.notification import NotificationChannel
from app.schemas.notification import (
    AlertNotificationPolicyCreate,
    AlertNotificationPolicyStepCreate,
    AlertNotificationPolicyUpdate,
    OnCallRotationCreate,
    OnCallRotationMemberCreate,
    OnCallRotationUpdate,
)
from app.services import notification as notification_service
from app.timezone import APP_TIMEZONE_NAME

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def alert_policies_list_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the alert notification policies list."""
    offset = (page - 1) * per_page
    policies = notification_service.alert_notification_policies.list(
        db=db,
        channel=None,
        status=None,
        severity_min=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    # Total count for pagination
    all_policies = notification_service.alert_notification_policies.list(
        db=db,
        channel=None,
        status=None,
        severity_min=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_policies)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "policies": policies,
        "channels": [c.value for c in NotificationChannel],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def alert_policy_detail_data(
    db: Session,
    *,
    policy_id: str,
) -> dict[str, object] | None:
    """Build template context for alert policy detail/edit."""
    policy = notification_service.alert_notification_policies.get(db, policy_id)
    if not policy:
        return None

    steps = notification_service.alert_notification_policy_steps.list(
        db=db,
        policy_id=policy_id,
        status=None,
        is_active=None,
        order_by="step_index",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    # Get templates and rotations for form dropdowns
    templates = notification_service.templates.list(
        db=db,
        channel=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    rotations = notification_service.on_call_rotations.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return {
        "policy": policy,
        "steps": steps,
        "notification_templates": templates,
        "rotations": rotations,
        "channels": [c.value for c in NotificationChannel],
    }


def alert_policy_form_data(db: Session) -> dict[str, object]:
    """Build template context for the new alert policy form."""
    templates = notification_service.templates.list(
        db=db,
        channel=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    rotations = notification_service.on_call_rotations.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return {
        "notification_templates": templates,
        "rotations": rotations,
        "channels": [c.value for c in NotificationChannel],
    }


def create_alert_policy(
    db: Session,
    *,
    name: str,
    channel: str,
    recipient: str,
    severity_min: str,
    template_id: str | None,
    notes: str | None,
    is_active: str | None,
):
    payload = AlertNotificationPolicyCreate(
        name=name.strip(),
        channel=NotificationChannel(channel),
        recipient=recipient.strip(),
        severity_min=AlertSeverity(severity_min),
        template_id=UUID(template_id) if template_id else None,
        notes=notes.strip() if notes else None,
        is_active=is_active is not None,
    )
    return notification_service.alert_notification_policies.create(
        db=db, payload=payload
    )


def update_alert_policy(
    db: Session,
    *,
    policy_id: UUID,
    name: str,
    channel: str,
    recipient: str,
    severity_min: str,
    template_id: str | None,
    notes: str | None,
    is_active: str | None,
):
    payload = AlertNotificationPolicyUpdate(
        name=name.strip(),
        channel=NotificationChannel(channel),
        recipient=recipient.strip(),
        severity_min=AlertSeverity(severity_min),
        template_id=UUID(template_id) if template_id else None,
        notes=notes.strip() if notes else None,
        is_active=is_active is not None,
    )
    return notification_service.alert_notification_policies.update(
        db=db, policy_id=str(policy_id), payload=payload
    )


def delete_alert_policy(db: Session, *, policy_id: UUID) -> None:
    notification_service.alert_notification_policies.delete(
        db=db, policy_id=str(policy_id)
    )


def create_alert_policy_step(
    db: Session,
    *,
    policy_id: UUID,
    step_index: int,
    delay_minutes: int,
    step_channel: str,
    step_recipient: str | None,
    step_rotation_id: str | None,
) -> None:
    try:
        payload = AlertNotificationPolicyStepCreate(
            policy_id=policy_id,
            step_index=step_index,
            delay_minutes=delay_minutes,
            channel=NotificationChannel(step_channel),
            recipient=step_recipient.strip() if step_recipient else None,
            rotation_id=UUID(step_rotation_id) if step_rotation_id else None,
        )
        notification_service.alert_notification_policy_steps.create(
            db=db, payload=payload
        )
    except Exception:
        logger.warning(
            "Failed to create alert policy step for policy %s",
            policy_id,
            exc_info=True,
        )


def delete_alert_policy_step(db: Session, *, step_id: UUID) -> None:
    notification_service.alert_notification_policy_steps.delete(
        db=db, step_id=str(step_id)
    )


def oncall_rotations_list_data(
    db: Session,
    *,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Build template context for the on-call rotations list."""
    offset = (page - 1) * per_page
    rotations = notification_service.on_call_rotations.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )
    all_rotations = notification_service.on_call_rotations.list(
        db=db,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    total = len(all_rotations)
    total_pages = (total + per_page - 1) // per_page if total else 1

    return {
        "rotations": rotations,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def oncall_rotation_detail_data(
    db: Session,
    *,
    rotation_id: str,
) -> dict[str, object] | None:
    """Build template context for on-call rotation detail."""
    rotation = notification_service.on_call_rotations.get(db, rotation_id)
    if not rotation:
        return None

    members = notification_service.on_call_rotation_members.list(
        db=db,
        rotation_id=rotation_id,
        is_active=None,
        order_by="priority",
        order_dir="asc",
        limit=500,
        offset=0,
    )

    return {
        "rotation": rotation,
        "members": members,
    }


def create_oncall_rotation(
    db: Session,
    *,
    name: str,
    timezone: str = APP_TIMEZONE_NAME,
    notes: str | None,
):
    payload = OnCallRotationCreate(
        name=name.strip(),
        timezone=timezone.strip(),
        notes=notes.strip() if notes else None,
    )
    return notification_service.on_call_rotations.create(db=db, payload=payload)


def update_oncall_rotation(
    db: Session,
    *,
    rotation_id: UUID,
    name: str,
    timezone: str = APP_TIMEZONE_NAME,
    notes: str | None,
    is_active: str | None,
):
    payload = OnCallRotationUpdate(
        name=name.strip(),
        timezone=timezone.strip(),
        notes=notes.strip() if notes else None,
        is_active=is_active is not None,
    )
    return notification_service.on_call_rotations.update(
        db=db, rotation_id=str(rotation_id), payload=payload
    )


def delete_oncall_rotation(db: Session, *, rotation_id: UUID) -> None:
    notification_service.on_call_rotations.delete(db=db, rotation_id=str(rotation_id))


def create_oncall_rotation_member(
    db: Session,
    *,
    rotation_id: UUID,
    member_name: str,
    member_contact: str,
    member_priority: int,
) -> None:
    try:
        payload = OnCallRotationMemberCreate(
            rotation_id=rotation_id,
            name=member_name.strip(),
            contact=member_contact.strip(),
            priority=member_priority,
        )
        notification_service.on_call_rotation_members.create(db=db, payload=payload)
    except Exception:
        logger.warning(
            "Failed to create on-call rotation member for rotation %s",
            rotation_id,
            exc_info=True,
        )


def delete_oncall_rotation_member(db: Session, *, member_id: UUID) -> None:
    notification_service.on_call_rotation_members.delete(
        db=db, member_id=str(member_id)
    )
