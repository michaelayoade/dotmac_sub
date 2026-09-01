"""Typed personal staff-notification menu and read-state owner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminNotification
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


@dataclass(frozen=True, slots=True)
class StaffNotificationMenuQuery:
    """Request the newest personal inbox items for one staff principal."""

    system_user_id: UUID
    limit: int = 10


@dataclass(frozen=True, slots=True)
class StaffNotificationMenuItem:
    id: UUID
    title: str
    body: str | None
    read_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StaffNotificationMenuProjection:
    items: tuple[StaffNotificationMenuItem, ...]
    unread_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class OpenStaffNotification:
    notification_id: UUID
    system_user_id: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class OpenStaffNotificationOutcome:
    notification_id: UUID
    opened: bool
    target_url: str | None
    read_at: datetime | None


_OPEN_STAFF_NOTIFICATION = OwnerCommandDefinition(
    owner="communications.staff_notification_read_state",
    concern="personal staff notification open and legacy target repair",
    name="open_staff_notification",
)


def _is_safe_admin_target(target_url: str) -> bool:
    return target_url == "/admin" or target_url.startswith("/admin/")


def _is_assignment_notification_title(title: str) -> bool:
    return title.startswith(("Ticket assigned: ", "Project assigned: "))


def _repair_legacy_assignment_target(
    db: Session,
    notification: AdminNotification,
) -> str:
    """Repair an old dashboard-only assignment target from its exact reference."""

    if notification.target_url != "/admin":
        return notification.target_url

    if notification.title.startswith("Ticket assigned: "):
        from app.models.support import Ticket

        reference = notification.title.removeprefix("Ticket assigned: ").strip()
        if not reference:
            return notification.target_url
        filters = [Ticket.number == reference]
        try:
            filters.append(Ticket.id == UUID(reference))
        except ValueError:
            pass
        ticket_matches = db.query(Ticket).filter(or_(*filters)).limit(2).all()
        if len(ticket_matches) == 1:
            notification.target_url = f"/admin/support/tickets/{ticket_matches[0].id}"
        return notification.target_url

    if notification.title.startswith("Project assigned: "):
        from app.models.project import Project

        reference = notification.title.removeprefix("Project assigned: ").strip()
        if not reference:
            return notification.target_url
        filters = [Project.number == reference]
        try:
            filters.append(Project.id == UUID(reference))
        except ValueError:
            pass
        project_matches = db.query(Project).filter(or_(*filters)).limit(2).all()
        if len(project_matches) == 1:
            notification.target_url = f"/admin/projects/{project_matches[0].id}"
        return notification.target_url

    return notification.target_url


def get_staff_notification_menu(
    db: Session,
    query: StaffNotificationMenuQuery,
) -> StaffNotificationMenuProjection:
    """Return a transaction-current, user-scoped menu and unread count."""

    limit = max(1, min(query.limit, 50))
    rows = (
        db.query(AdminNotification)
        .filter(AdminNotification.system_user_id == query.system_user_id)
        .order_by(AdminNotification.created_at.desc())
        .limit(limit)
        .all()
    )
    unread_count = (
        db.query(func.count(AdminNotification.id))
        .filter(AdminNotification.system_user_id == query.system_user_id)
        .filter(AdminNotification.read_at.is_(None))
        .scalar()
        or 0
    )
    return StaffNotificationMenuProjection(
        items=tuple(
            StaffNotificationMenuItem(
                id=row.id,
                title=row.title,
                body=row.body,
                read_at=row.read_at,
                created_at=row.created_at,
            )
            for row in rows
        ),
        unread_count=int(unread_count),
        observed_at=datetime.now(UTC),
    )


def open_staff_notification(
    db: Session,
    command: OpenStaffNotification,
) -> OpenStaffNotificationOutcome:
    """Mark one personal notification read and return its safe navigation target."""

    def operation() -> OpenStaffNotificationOutcome:
        notification = (
            db.query(AdminNotification)
            .filter(AdminNotification.id == command.notification_id)
            .filter(AdminNotification.system_user_id == command.system_user_id)
            .one_or_none()
        )
        if notification is None:
            return OpenStaffNotificationOutcome(
                notification_id=command.notification_id,
                opened=False,
                target_url=None,
                read_at=None,
            )
        target_url = _repair_legacy_assignment_target(db, notification)
        if not _is_safe_admin_target(target_url) or (
            target_url == "/admin"
            and _is_assignment_notification_title(notification.title)
        ):
            return OpenStaffNotificationOutcome(
                notification_id=notification.id,
                opened=False,
                target_url=None,
                read_at=notification.read_at,
            )
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            db.flush()
            emit_event(
                db,
                EventType.staff_notification_opened,
                {
                    "notification_id": str(notification.id),
                    "system_user_id": str(command.system_user_id),
                },
                actor=command.context.actor,
            )
        return OpenStaffNotificationOutcome(
            notification_id=notification.id,
            opened=True,
            target_url=target_url,
            read_at=notification.read_at,
        )

    return execute_owner_command(
        db,
        definition=_OPEN_STAFF_NOTIFICATION,
        context=command.context,
        operation=operation,
    )
