"""Service helpers for admin system audit pages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.subscriber import Subscriber
from app.services import audit as audit_service
from app.services.audit_helpers import (
    extract_changes,
    format_audit_datetime,
    format_changes,
    humanize_action,
    humanize_entity,
)


def get_audit_page_data(
    db: Session,
    *,
    actor_id: str | None,
    action: str | None,
    entity_type: str | None,
    page: int,
    per_page: int,
) -> dict[str, object]:
    """Return audit events view rows and pagination totals."""
    offset = (page - 1) * per_page

    events = audit_service.audit_events.list(
        db=db,
        actor_id=str(UUID(actor_id)) if actor_id else None,
        actor_type=None,
        action=action if action else None,
        entity_type=entity_type if entity_type else None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    actor_ids = {
        event.actor_id
        for event in events
        if event.actor_id and _is_user_actor(getattr(event, "actor_type", None))
    }
    people: dict[str, Subscriber] = {}
    if actor_ids:
        try:
            people = {
                str(person.id): person
                for person in db.execute(
                    select(Subscriber).where(Subscriber.id.in_(actor_ids))
                )
                .scalars()
                .all()
            }
        except Exception:
            people = {}

    event_views = []
    for event in events:
        actor_name = _resolve_actor_name(event, people)
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(event.action)
        entity_label = humanize_entity(event.entity_type, event.entity_id)
        event_views.append(
            {
                "occurred_at": event.occurred_at,
                "occurred_at_display": format_audit_datetime(
                    event.occurred_at, "%b %d, %Y %H:%M"
                ),
                "actor_name": actor_name,
                "actor_id": event.actor_id,
                "action": event.action,
                "action_label": action_label,
                "action_detail": change_summary,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "entity_label": entity_label,
                "is_success": event.is_success,
                "status_code": event.status_code,
            }
        )

    total_stmt = select(func.count()).select_from(AuditEvent).where(
        AuditEvent.is_active.is_(True)
    )
    if actor_id:
        total_stmt = total_stmt.where(AuditEvent.actor_id == UUID(actor_id))
    if action:
        total_stmt = total_stmt.where(AuditEvent.action == action)
    if entity_type:
        total_stmt = total_stmt.where(AuditEvent.entity_type == entity_type)

    total = db.scalar(total_stmt) or 0
    total_pages = (total + per_page - 1) // per_page

    return {
        "events": event_views,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def _is_user_actor(actor_type) -> bool:
    return actor_type in {AuditActorType.user, AuditActorType.user.value, "user"}


def _resolve_actor_name(event, people: dict[str, Subscriber]) -> str:
    actor_name = None
    is_user_actor = _is_user_actor(getattr(event, "actor_type", None))
    if event.actor_id and is_user_actor:
        actor = people.get(str(event.actor_id))
        if actor:
            actor_name = (
                actor.display_name
                or f"{actor.first_name} {actor.last_name}".strip()
                or actor.email
            )
    if actor_name:
        return actor_name
    metadata = getattr(event, "metadata_", None) or {}
    if is_user_actor:
        return metadata.get("actor_email") or event.actor_id or "User"
    return metadata.get("actor_name") or metadata.get("actor_email") or event.actor_id or "System"
