"""Service helpers for notification alert-policy and on-call web routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.notification import NotificationChannel
from app.services import notification as notification_service

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
