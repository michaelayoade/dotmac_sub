from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.support import Ticket
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationAssignment,
    InboxConversationStatus,
)
from app.models.work_order_mirror import WorkOrderMirror
from app.models.workqueue import WorkqueueItemKind, WorkqueueSnooze
from app.services.common import coerce_uuid


@dataclass(frozen=True)
class WorkqueueItem:
    item_kind: str
    item_id: UUID
    title: str
    subtitle: str | None
    status: str
    priority: int
    due_at: datetime | None = None
    last_activity_at: datetime | None = None
    subscriber_id: UUID | None = None
    service_team_id: UUID | None = None
    assigned_person_id: UUID | None = None
    url: str | None = None
    metadata: dict = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _priority_score(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    normalized = str(value or "").lower()
    return {
        "urgent": 10,
        "high": 20,
        "normal": 40,
        "medium": 40,
        "low": 70,
        "lower": 80,
    }.get(normalized, 50)


def _active_snooze_keys(
    db: Session,
    *,
    user_id: UUID,
    now: datetime,
) -> set[tuple[str, UUID]]:
    rows = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == user_id)
        .filter(
            or_(
                WorkqueueSnooze.snooze_until.is_(None),
                WorkqueueSnooze.snooze_until > now,
                WorkqueueSnooze.until_next_reply.is_(True),
            )
        )
        .all()
    )
    return {(row.item_kind, row.item_id) for row in rows}


def _conversation_items(
    db: Session,
    *,
    user_id: UUID,
    service_team_id: UUID | None,
    limit: int,
) -> list[WorkqueueItem]:
    query = (
        db.query(InboxConversation, InboxConversationAssignment)
        .outerjoin(
            InboxConversationAssignment,
            (InboxConversationAssignment.conversation_id == InboxConversation.id)
            & (InboxConversationAssignment.is_active.is_(True)),
        )
        .filter(InboxConversation.is_active.is_(True))
        .filter(InboxConversation.status != InboxConversationStatus.resolved.value)
    )
    if service_team_id is not None:
        query = query.filter(
            InboxConversation.primary_service_team_id == service_team_id
        )
    query = query.filter(
        or_(
            InboxConversationAssignment.person_id == user_id,
            InboxConversationAssignment.person_id.is_(None),
        )
    )
    rows = (
        query.order_by(
            InboxConversation.priority.asc(),
            InboxConversation.last_message_at.desc().nullslast(),
        )
        .limit(limit)
        .all()
    )
    return [
        WorkqueueItem(
            item_kind=WorkqueueItemKind.conversation.value,
            item_id=conversation.id,
            title=conversation.subject or "Inbox conversation",
            subtitle=conversation.contact_address,
            status=conversation.status,
            priority=conversation.priority,
            last_activity_at=conversation.last_message_at,
            subscriber_id=conversation.subscriber_id,
            service_team_id=conversation.primary_service_team_id,
            assigned_person_id=assignment.person_id if assignment else None,
            url=f"/admin/inbox/{conversation.id}",
            metadata={"channel_type": conversation.channel_type},
        )
        for conversation, assignment in rows
    ]


def _ticket_items(
    db: Session,
    *,
    user_id: UUID,
    service_team_id: UUID | None,
    limit: int,
) -> list[WorkqueueItem]:
    query = db.query(Ticket).filter(Ticket.is_active.is_(True))
    query = query.filter(
        Ticket.status.notin_(["closed", "canceled", "resolved", "merged"])
    )
    query = query.filter(
        or_(
            Ticket.assigned_to_person_id == user_id,
            Ticket.assigned_to_person_id.is_(None),
        )
    )
    if service_team_id is not None:
        query = query.filter(Ticket.service_team_id == service_team_id)
    rows = (
        query.order_by(Ticket.due_at.asc().nullslast(), Ticket.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        WorkqueueItem(
            item_kind=WorkqueueItemKind.ticket.value,
            item_id=ticket.id,
            title=ticket.title,
            subtitle=ticket.number,
            status=ticket.status,
            priority=_priority_score(ticket.priority),
            due_at=ticket.due_at,
            last_activity_at=ticket.updated_at,
            subscriber_id=ticket.subscriber_id,
            service_team_id=ticket.service_team_id,
            assigned_person_id=ticket.assigned_to_person_id,
            url=f"/admin/support/tickets/{ticket.id}",
            metadata={"ticket_type": ticket.ticket_type},
        )
        for ticket in rows
    ]


def _work_order_items(
    db: Session,
    *,
    service_team_id: UUID | None,
    limit: int,
) -> list[WorkqueueItem]:
    del service_team_id
    rows = (
        db.query(WorkOrderMirror)
        .filter(WorkOrderMirror.is_active.is_(True))
        .filter(WorkOrderMirror.status.notin_(["completed", "canceled"]))
        .order_by(
            WorkOrderMirror.scheduled_start.asc().nullslast(),
            WorkOrderMirror.updated_at.desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        WorkqueueItem(
            item_kind=WorkqueueItemKind.work_order.value,
            item_id=work_order.id,
            title=work_order.title or "Work order",
            subtitle=work_order.assigned_to_name or work_order.technician_name,
            status=work_order.status,
            priority=_priority_score(work_order.priority),
            due_at=work_order.scheduled_start,
            last_activity_at=work_order.updated_at,
            subscriber_id=work_order.subscriber_id,
            service_team_id=None,
            assigned_person_id=None,
            url=f"/admin/work-orders/{work_order.id}",
            metadata={
                "work_type": work_order.work_type,
                "crm_work_order_id": work_order.crm_work_order_id,
            },
        )
        for work_order in rows
    ]


def list_workqueue(
    db: Session,
    *,
    user_id: str | UUID,
    service_team_id: str | UUID | None = None,
    include_snoozed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[WorkqueueItem]:
    user_uuid = coerce_uuid(user_id)
    team_uuid = coerce_uuid(service_team_id) if service_team_id else None
    current_time = _now()
    per_source_limit = max(limit + offset, 20)
    items = [
        *_conversation_items(
            db, user_id=user_uuid, service_team_id=team_uuid, limit=per_source_limit
        ),
        *_ticket_items(
            db, user_id=user_uuid, service_team_id=team_uuid, limit=per_source_limit
        ),
        *_work_order_items(db, service_team_id=team_uuid, limit=per_source_limit),
    ]
    if not include_snoozed:
        snoozed = _active_snooze_keys(db, user_id=user_uuid, now=current_time)
        items = [
            item for item in items if (item.item_kind, item.item_id) not in snoozed
        ]
    items.sort(
        key=lambda item: (
            item.priority,
            item.due_at or datetime.max.replace(tzinfo=UTC),
            item.last_activity_at or datetime.min.replace(tzinfo=UTC),
        )
    )
    return items[offset : offset + limit]


def snooze_item(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    user_uuid = coerce_uuid(user_id)
    item_uuid = coerce_uuid(item_id)
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == user_uuid)
        .filter(WorkqueueSnooze.item_kind == item_kind)
        .filter(WorkqueueSnooze.item_id == item_uuid)
        .one_or_none()
    )
    if snooze is None:
        snooze = WorkqueueSnooze(
            user_id=user_uuid,
            item_kind=item_kind,
            item_id=item_uuid,
        )
        db.add(snooze)
    snooze.snooze_until = snooze_until
    snooze.until_next_reply = until_next_reply
    db.flush()
    return snooze


def clear_snooze(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str,
    item_id: str | UUID,
) -> None:
    snooze = (
        db.query(WorkqueueSnooze)
        .filter(WorkqueueSnooze.user_id == coerce_uuid(user_id))
        .filter(WorkqueueSnooze.item_kind == item_kind)
        .filter(WorkqueueSnooze.item_id == coerce_uuid(item_id))
        .one_or_none()
    )
    if snooze is None:
        raise HTTPException(status_code=404, detail="Snooze not found")
    db.delete(snooze)
    db.flush()


# Commit-owning entry points — see the SOT service-ownership contract; the API
# layer calls these rather than committing itself.
def snooze_item_committed(
    db: Session,
    *,
    user_id: str | UUID,
    item_kind: str,
    item_id: str | UUID,
    snooze_until: datetime | None = None,
    until_next_reply: bool = False,
) -> WorkqueueSnooze:
    snooze = snooze_item(
        db,
        user_id=user_id,
        item_kind=item_kind,
        item_id=item_id,
        snooze_until=snooze_until,
        until_next_reply=until_next_reply,
    )
    db.commit()
    db.refresh(snooze)
    return snooze


def clear_snooze_committed(
    db: Session, *, user_id: str | UUID, item_kind: str, item_id: str | UUID
) -> None:
    clear_snooze(db, user_id=user_id, item_kind=item_kind, item_id=item_id)
    db.commit()
