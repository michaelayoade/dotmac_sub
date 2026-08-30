"""Shared queue helpers for staff/internal notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminNotification
from app.models.network_monitoring import AlertSeverity
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.system_user import SystemUser
from app.schemas.notification import NotificationCreate
from app.services import admin_alerts
from app.services.branding_config import get_brand
from app.services.domain_errors import DomainError
from app.services.notification import notifications as notifications_svc


@dataclass(frozen=True, slots=True)
class PermissionReviewNotificationResult:
    """Materialized audience and channels for one staff review request."""

    alert_status: str
    target_count: int
    inbox_count: int
    email_count: int
    whatsapp_count: int
    sla_policy_count: int
    sla_delivery_count: int


class StaffDirectEventType(StrEnum):
    """Bounded staff-only events that create a personal notification."""

    team_inbox_private_note_mention = "team_inbox.private_note_mention"


class StaffNotificationError(DomainError):
    """Safe rejection from the typed staff-notification participant."""


@dataclass(frozen=True, slots=True)
class StageStaffDirectNotification:
    """Typed request to stage one staff member's in-app and email notice."""

    system_user_id: UUID
    source_event_id: UUID
    event_type: StaffDirectEventType
    subject: str
    body: str
    target_url: str


@dataclass(frozen=True, slots=True)
class StaffDirectNotificationResult:
    admin_notification_id: UUID
    push_notification_id: UUID
    email_notification_id: UUID | None


def _direct_notification_dedupe_key(
    command: StageStaffDirectNotification,
    channel: NotificationChannel,
) -> str:
    return ":".join(
        (
            "staff-direct",
            command.event_type.value,
            str(command.source_event_id),
            str(command.system_user_id),
            channel.value,
        )
    )


def _absolute_staff_url(target_url: str) -> str:
    value = str(target_url or "").strip()
    if value.startswith(("https://", "http://")):
        return value
    app_url = str(get_brand().get("app_url") or "").rstrip("/")
    return f"{app_url}/{value.lstrip('/')}" if app_url else value


def stage_staff_direct_notification(
    db: Session,
    command: StageStaffDirectNotification,
) -> StaffDirectNotificationResult:
    """Stage idempotent in-app and email notices in the caller's transaction."""

    if (
        not command.subject.strip()
        or not command.body.strip()
        or not command.target_url
    ):
        raise StaffNotificationError(
            code="communications.staff_notifications.invalid_request",
            message="Staff notification content and target are required.",
        )
    user = db.get(SystemUser, command.system_user_id)
    if user is None or not user.is_active:
        raise StaffNotificationError(
            code="communications.staff_notifications.invalid_recipient",
            message="Active staff notification recipient not found.",
        )

    push_dedupe_key = _direct_notification_dedupe_key(command, NotificationChannel.push)
    push_notification = (
        db.query(Notification)
        .filter(
            Notification.channel == NotificationChannel.push,
            Notification.dedupe_key == push_dedupe_key,
        )
        .one_or_none()
    )
    if push_notification is None:
        push_notification = queue_staff_notification(
            db,
            channel=NotificationChannel.push,
            recipient=str(user.id),
            subject=command.subject,
            body=command.body,
            delivered=True,
            event_type=command.event_type.value,
            category="support",
            audience_type="system_user",
            audience_id=user.id,
            metadata={
                "source_event_id": str(command.source_event_id),
                "target_url": command.target_url,
                "internal_only": True,
            },
        )
        if push_notification is None:  # pragma: no cover - recipient is validated.
            raise RuntimeError("Staff in-app notification was not staged")
        push_notification.dedupe_key = push_dedupe_key

    admin_notification = (
        db.query(AdminNotification)
        .filter(AdminNotification.source_notification_id == push_notification.id)
        .one_or_none()
    )
    if admin_notification is None:
        admin_notification = AdminNotification(
            source_notification_id=push_notification.id,
            system_user_id=user.id,
            title=command.subject[:180],
            body=command.body,
            target_url=command.target_url,
        )
        db.add(admin_notification)
        db.flush()

    email_notification: Notification | None = None
    if user.email:
        email_dedupe_key = _direct_notification_dedupe_key(
            command, NotificationChannel.email
        )
        email_notification = (
            db.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.email,
                Notification.dedupe_key == email_dedupe_key,
            )
            .one_or_none()
        )
        if email_notification is None:
            open_url = f"/admin/notifications/inbox/{admin_notification.id}/open"
            email_notification = queue_staff_notification(
                db,
                channel=NotificationChannel.email,
                recipient=user.email,
                subject=command.subject,
                body=(
                    f"{command.body}\n\nOpen the note: {_absolute_staff_url(open_url)}"
                ),
                event_type=command.event_type.value,
                category="support",
                audience_type="system_user",
                audience_id=user.id,
                metadata={
                    "source_event_id": str(command.source_event_id),
                    "target_url": open_url,
                    "internal_only": True,
                },
            )
            if email_notification is not None:
                email_notification.dedupe_key = email_dedupe_key

    db.flush()
    return StaffDirectNotificationResult(
        admin_notification_id=admin_notification.id,
        push_notification_id=push_notification.id,
        email_notification_id=(
            email_notification.id if email_notification is not None else None
        ),
    )


def resolve_assignment_users(
    db: Session,
    *,
    person_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
    service_team_ids: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> list[SystemUser]:
    """Resolve active users assigned directly or through active service teams."""
    normalized_people = {str(value) for value in person_ids if value}
    normalized_teams = {str(value) for value in service_team_ids if value}
    user_ids: set[UUID] = set()
    if normalized_people:
        direct = (
            db.query(SystemUser.id)
            .filter(SystemUser.is_active.is_(True))
            .filter(
                or_(
                    SystemUser.id.in_(normalized_people),
                    SystemUser.person_party_id.in_(normalized_people),
                )
            )
            .all()
        )
        user_ids.update(row[0] for row in direct)
    if normalized_teams:
        team_members = (
            db.query(SystemUser.id)
            .select_from(ServiceTeamMember)
            .join(ServiceTeam, ServiceTeam.id == ServiceTeamMember.team_id)
            .join(SystemUser, SystemUser.person_party_id == ServiceTeamMember.person_id)
            .filter(ServiceTeam.id.in_(normalized_teams))
            .filter(ServiceTeam.is_active.is_(True))
            .filter(ServiceTeamMember.is_active.is_(True))
            .filter(SystemUser.is_active.is_(True))
            .all()
        )
        user_ids.update(row[0] for row in team_members)
    if not user_ids:
        return []
    return (
        db.query(SystemUser)
        .filter(SystemUser.id.in_(user_ids))
        .order_by(SystemUser.id.asc())
        .all()
    )


def queue_staff_assignment_notifications(
    db: Session,
    *,
    users: list[SystemUser],
    subject: str,
    body: str,
    actor_id: str | None = None,
) -> tuple[str, ...]:
    """Queue in-app and email assignment notifications for active staff."""
    notified: list[str] = []
    for user in users:
        if actor_id and str(user.id) == str(actor_id):
            continue
        queue_staff_push(
            db,
            recipient=str(user.id),
            subject=subject,
            body=body,
        )
        if user.email:
            queue_staff_email(
                db,
                recipient=user.email,
                subject=subject,
                body=body,
            )
        notified.append(str(user.id))
    return tuple(notified)


def queue_staff_notification(
    db: Session,
    *,
    channel: NotificationChannel,
    recipient: str,
    subject: str,
    body: str,
    delivered: bool = False,
    sent_at: datetime | None = None,
    event_type: str | None = None,
    category: str | None = None,
    audience_type: str | None = None,
    audience_id=None,
    metadata: dict | None = None,
) -> Notification | None:
    """Queue an internal notification without customer preference/status policy."""
    if not recipient:
        return None
    return notifications_svc.queue_internal_notification(
        db,
        NotificationCreate(
            channel=channel,
            recipient=recipient,
            subject=subject,
            body=body,
            event_type=event_type,
            category=category,
            audience_type=audience_type,
            audience_id=audience_id,
            metadata_=metadata or {},
            status=NotificationStatus.delivered
            if delivered
            else NotificationStatus.queued,
            sent_at=sent_at or (datetime.now(UTC) if delivered else None),
        ),
    )


def queue_staff_push(
    db: Session,
    *,
    recipient: str,
    subject: str,
    body: str,
    delivered: bool = True,
    target_url: str = "/admin",
) -> None:
    notification = queue_staff_notification(
        db,
        channel=NotificationChannel.push,
        recipient=recipient,
        subject=subject,
        body=body,
        delivered=delivered,
        metadata={"target_url": target_url},
    )
    if notification is None:
        return
    try:
        system_user_id = UUID(str(recipient))
    except (TypeError, ValueError):
        return
    user_exists = (
        db.query(SystemUser.id)
        .filter(SystemUser.id == system_user_id)
        .filter(SystemUser.is_active.is_(True))
        .first()
    )
    if user_exists is None:
        return
    db.add(
        AdminNotification(
            source_notification_id=notification.id,
            system_user_id=system_user_id,
            title=subject[:180],
            body=body,
            target_url=target_url,
        )
    )


def queue_staff_email(
    db: Session,
    *,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    queue_staff_notification(
        db,
        channel=NotificationChannel.email,
        recipient=recipient,
        subject=subject,
        body=body,
    )


def _grant_keys_for(permission_key: str) -> frozenset[str]:
    """Permission grants that effectively satisfy one required permission."""
    parts = permission_key.split(":")
    keys = {permission_key, "*"}
    for index in range(1, len(parts)):
        keys.add(":".join(parts[:index]) + ":*")
    return frozenset(keys)


def system_users_with_permission(db: Session, permission_key: str) -> list[SystemUser]:
    """Resolve active staff who can execute a permission-gated review.

    This mirrors the effective grant semantics used by ``require_permission``:
    exact, ancestor wildcard, global wildcard, direct grants, role grants and
    the active ``admin`` role all qualify.
    """
    grant_keys = _grant_keys_for(permission_key)
    role_target_ids = (
        db.query(SystemUser.id)
        .join(SystemUserRole, SystemUserRole.system_user_id == SystemUser.id)
        .join(Role, Role.id == SystemUserRole.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .filter(SystemUser.is_active.is_(True))
        .filter(Role.is_active.is_(True))
        .filter(
            or_(
                Role.name == "admin",
                and_(
                    Permission.is_active.is_(True),
                    Permission.key.in_(grant_keys),
                ),
            )
        )
    )
    direct_target_ids = (
        db.query(SystemUser.id)
        .join(
            SystemUserPermission,
            SystemUserPermission.system_user_id == SystemUser.id,
        )
        .join(Permission, Permission.id == SystemUserPermission.permission_id)
        .filter(SystemUser.is_active.is_(True))
        .filter(Permission.is_active.is_(True))
        .filter(Permission.key.in_(grant_keys))
    )
    target_ids = {row[0] for row in role_target_ids.union(direct_target_ids).all()}
    if not target_ids:
        return []
    return (
        db.query(SystemUser)
        .filter(SystemUser.id.in_(target_ids))
        .order_by(SystemUser.email.asc(), SystemUser.id.asc())
        .all()
    )


def queue_permission_review_request(
    db: Session,
    *,
    permission_key: str,
    fingerprint: str,
    event_type: str,
    title: str,
    body: str,
    target_url: str,
    category: str,
    source: str,
    sla_entity_type: str | None = None,
    sla_entity_id: str | None = None,
    sla_trigger: str | None = None,
) -> PermissionReviewNotificationResult:
    """Place a review request in authorized staff inboxes and configured channels.

    The in-app inbox is canonical and immediate. Timed escalation and external
    delivery are planned only from active operational SLA policies; this owner
    contains no fallback timing or channel list.
    """
    targets = system_users_with_permission(db, permission_key)
    alert_status = admin_alerts.sync_alert(
        db,
        admin_alerts.AlertFinding(
            fingerprint=fingerprint,
            category=category,
            source=source,
            severity=AlertSeverity.warning,
            title=title,
            summary=body[:255],
            details={
                "permission_key": permission_key,
                "event_type": event_type,
            },
            target_url=target_url,
        ),
        target_users=targets,
    )
    sla_policy_count, sla_delivery_count = _plan_permission_sla_escalations(
        db,
        targets=targets,
        permission_key=permission_key,
        sla_entity_type=sla_entity_type,
        sla_entity_id=sla_entity_id,
        sla_trigger=sla_trigger,
        title=title,
        body=body,
        target_url=target_url,
        category=category,
        source=source,
    )
    return PermissionReviewNotificationResult(
        alert_status=alert_status,
        target_count=len(targets),
        inbox_count=len(targets) if alert_status in {"opened", "escalated"} else 0,
        email_count=0,
        whatsapp_count=0,
        sla_policy_count=sla_policy_count,
        sla_delivery_count=sla_delivery_count,
    )


def _plan_permission_sla_escalations(
    db: Session,
    *,
    targets: list[SystemUser],
    permission_key: str,
    sla_entity_type: str | None,
    sla_entity_id: str | None,
    sla_trigger: str | None,
    title: str,
    body: str,
    target_url: str,
    category: str,
    source: str,
) -> tuple[int, int]:
    supplied = (sla_entity_type, sla_entity_id, sla_trigger)
    if not any(supplied):
        return 0, 0
    if not all(supplied):
        raise ValueError(
            "SLA entity type, entity ID and trigger must be supplied together"
        )

    from app.services import operational_escalation

    assert sla_entity_type is not None
    assert sla_entity_id is not None
    assert sla_trigger is not None
    policies = operational_escalation.matching_policies(
        db,
        entity_type=sla_entity_type,
        trigger=sla_trigger,
        severity="warning",
    )
    if not policies:
        return 0, 0

    from app.services.branding_config import get_brand

    app_url = str(get_brand().get("app_url") or "").rstrip("/")
    delivery_target = f"{app_url}{target_url}" if app_url else target_url
    delivery_body = f"{body}\n\nOpen: {delivery_target}"

    for user in targets:
        operational_escalation.add_watcher(
            db,
            entity_type=sla_entity_type,
            entity_id=sla_entity_id,
            person_id=user.id,
            source=source,
            reason=f"Authorized by {permission_key}",
            metadata={"permission_key": permission_key},
        )

    result = operational_escalation.emit_sla_event(
        db,
        entity_type=sla_entity_type,
        entity_id=sla_entity_id,
        trigger=sla_trigger,
        severity="warning",
        metadata={
            "permission_key": permission_key,
            "title": title,
            "body": delivery_body,
            "target_url": target_url,
            "category": category,
            "source": source,
        },
        policies=policies,
    )
    return result.policy_count, len(result.deliveries)


def resolve_permission_review_request(
    db: Session,
    *,
    fingerprint: str,
    event_type: str,
    sla_entity_type: str | None = None,
    sla_entity_id: str | None = None,
    sla_trigger: str | None = None,
) -> bool:
    """Close an in-app review request and cancel undelivered fast channels."""
    alert = admin_alerts.resolve_alert_by_fingerprint(
        db,
        fingerprint,
        mark_notifications_read=True,
    )
    (
        db.query(Notification)
        .filter(Notification.event_type == event_type)
        .filter(
            Notification.status.in_(
                (NotificationStatus.queued, NotificationStatus.failed)
            )
        )
        .update(
            {
                "status": NotificationStatus.canceled,
                "last_error": "review_completed_before_delivery",
            },
            synchronize_session=False,
        )
    )
    if sla_entity_type and sla_entity_id:
        from app.services import operational_escalation

        operational_escalation.cancel_entity_events(
            db,
            entity_type=sla_entity_type,
            entity_id=sla_entity_id,
            trigger=sla_trigger,
            reason="review_completed_before_escalation",
        )
    return alert is not None
