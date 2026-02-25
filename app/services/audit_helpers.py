from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.services import audit as audit_service

SENSITIVE_FIELDS = {
    "password",
    "password_hash",
    "hashed_password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "private_key",
    "salt",
}

AUDIT_TIMEZONE = ZoneInfo("Africa/Lagos")


def _normalize_value(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _to_audit_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(AUDIT_TIMEZONE)


def format_audit_datetime(value: datetime | None, fmt: str) -> str:
    localized = _to_audit_timezone(value)
    if not localized:
        return "Unknown"
    return localized.strftime(fmt)


def model_to_dict(model, include: set[str] | None = None, exclude: set[str] | None = None) -> dict:
    if model is None:
        return {}
    excluded = set(exclude or set()) | SENSITIVE_FIELDS
    data: dict[str, object] = {}
    for attr in inspect(model).mapper.column_attrs:
        key = attr.key
        if include and key not in include:
            continue
        if key in excluded:
            continue
        data[key] = _normalize_value(getattr(model, key))
    return data


def diff_dicts(before: dict, after: dict) -> dict:
    changes: dict[str, dict[str, object]] = {}
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        before_val = before.get(key)
        after_val = after.get(key)
        if before_val != after_val:
            changes[key] = {"from": before_val, "to": after_val}
    return changes


def build_changes_metadata(
    before_model,
    after_model,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> dict | None:
    before = model_to_dict(before_model, include=include, exclude=exclude)
    after = model_to_dict(after_model, include=include, exclude=exclude)
    changes = diff_dicts(before, after)
    if not changes:
        return None
    return {"changes": changes}


def format_changes(changes: dict | None, max_items: int = 3) -> str | None:
    if not changes:
        return None
    items = []
    for idx, (field, value) in enumerate(changes.items()):
        if idx >= max_items:
            break
        before_val = value.get("from")
        after_val = value.get("to")
        items.append(f"{field}: {before_val} -> {after_val}")
    if not items:
        return None
    suffix = "…" if len(changes) > max_items else ""
    return "; ".join(items) + suffix


def extract_changes(
    metadata: Mapping[str, Any] | None, action: str | None = None
) -> dict[str, Any] | None:
    if not metadata:
        return None
    changes = metadata.get("changes")
    if isinstance(changes, dict) and changes:
        return dict(changes)
    if "from" in metadata and "to" in metadata:
        field = "value"
        if action == "status_change":
            field = "status"
        elif action == "priority_change":
            field = "priority"
        return {field: {"from": metadata.get("from"), "to": metadata.get("to")}}
    return None


def humanize_action(action: str | None) -> str:
    if not action:
        return "Activity"
    method_map = {
        "GET": "Viewed",
        "POST": "Created",
        "PUT": "Updated",
        "PATCH": "Updated",
        "DELETE": "Deleted",
    }
    if action.upper() in method_map:
        return method_map[action.upper()]
    return action.replace("_", " ").replace("-", " ").title()


def humanize_entity(entity_type: str | None, entity_id: str | None = None) -> str:
    if not entity_type:
        return "Item"
    label = entity_type
    if entity_type.startswith("/"):
        parts = [p for p in entity_type.split("/") if p]
        cleaned = []
        for part in parts:
            if part in {"admin", "api", "system", "portal", "vendor", "reseller"}:
                continue
            if part.isdigit():
                continue
            cleaned.append(part)
        label = " ".join(cleaned) if cleaned else entity_type.strip("/")
    label = label.replace("_", " ").replace("-", " ").title()
    if entity_id:
        short_id = str(entity_id)[:8]
        return f"{label} #{short_id}"
    return label


def log_audit_event(
    db: Session,
    request,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor_id: str | None,
    metadata: dict | None = None,
    status_code: int = 200,
    is_success: bool = True,
) -> None:
    actor_type = AuditActorType.user if actor_id else AuditActorType.system
    metadata_payload = dict(metadata or {})
    if request is not None:
        subscriber = getattr(request.state, "user", None)
        if subscriber:
            display_name = subscriber.display_name or f"{subscriber.first_name} {subscriber.last_name}".strip()
            if display_name and not metadata_payload.get("actor_name"):
                metadata_payload["actor_name"] = display_name
            if getattr(subscriber, "email", None) and not metadata_payload.get("actor_email"):
                metadata_payload["actor_email"] = subscriber.email
    payload = AuditEventCreate(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        status_code=status_code,
        is_success=is_success,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        request_id=request.headers.get("x-request-id") if request else None,
        metadata_=metadata_payload or None,
    )
    audit_service.audit_events.create(db=db, payload=payload)


def build_audit_activities(
    db: Session,
    entity_type: str,
    entity_id: str,
    limit: int = 10,
) -> list[dict]:
    """Build activity feed for a single entity type + id."""
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=limit,
        offset=0,
    )
    return _events_to_activities(db, events)


def build_audit_activities_for_types(
    db: Session,
    entity_types: list[str],
    limit: int = 10,
) -> list[dict]:
    """Build activity feed across multiple entity types."""
    if not entity_types:
        return []
    from app.models.audit import AuditEvent

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type.in_(entity_types))
        .filter(AuditEvent.is_active.is_(True))
        .order_by(AuditEvent.occurred_at.desc())
        .limit(limit)
        .all()
    )
    return _events_to_activities(db, events, include_entity_label=True)


def _events_to_activities(
    db: Session,
    events: list,
    include_entity_label: bool = False,
) -> list[dict]:
    """Shared logic: resolve actors and build activity dicts from audit events."""
    if not events:
        return []
    actor_ids = {
        str(event.actor_id)
        for event in events
        if getattr(event, "actor_id", None)
    }
    people: dict[str, Subscriber] = {}
    if actor_ids:
        people = {
            str(person.id): person
            for person in db.query(Subscriber)
            .filter(Subscriber.id.in_(actor_ids))
            .all()
        }
    activities: list[dict] = []
    for event in events:
        actor = (
            people.get(str(event.actor_id))
            if getattr(event, "actor_id", None)
            else None
        )
        actor_name = (
            f"{actor.first_name} {actor.last_name}".strip()
            if actor
            else "System"
        )
        metadata = getattr(event, "metadata_", None) or {}
        comment_text = str(metadata.get("comment") or "").strip()
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes, max_items=2)
        action_label = (event.action or "Activity").replace("_", " ").title()
        if include_entity_label:
            entity_label = (
                (event.entity_type or "Activity").replace("_", " ").title()
            )
            title = f"{entity_label} {action_label}"
        else:
            title = action_label
        if comment_text:
            description = f"{actor_name} · {comment_text}"
        else:
            description = f"{actor_name}" + (f" · {change_summary}" if change_summary else "")
        activities.append(
            {
                "title": title,
                "description": description,
                "occurred_at": event.occurred_at,
            }
        )
    return activities


def log_update(
    db: Session,
    request,
    entity_type: str,
    entity_id: str,
    before_obj,
    after_obj,
    actor_id: str | None,
    exclude_fields: set[str] | None = None,
) -> None:
    """Snapshot before/after, compute diff, and log an audit event in one call."""
    meta = build_changes_metadata(
        before_obj, after_obj, exclude=exclude_fields
    )
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        metadata=meta,
    )


def _is_user_actor(actor_type) -> bool:
    return actor_type in {AuditActorType.user, AuditActorType.user.value, "user"}


def _resolve_actor_name(event, subscribers: dict[str, Subscriber]) -> str:
    actor_id = getattr(event, "actor_id", None)
    actor_type = getattr(event, "actor_type", None)
    if actor_id and _is_user_actor(actor_type):
        subscriber = subscribers.get(str(actor_id))
        if subscriber:
            return (
                subscriber.display_name
                or f"{subscriber.first_name} {subscriber.last_name}".strip()
                or subscriber.email
            )
        metadata = getattr(event, "metadata_", None) or {}
        return metadata.get("actor_email") or str(actor_id)
    metadata = getattr(event, "metadata_", None) or {}
    return (
        metadata.get("actor_name")
        or metadata.get("actor_email")
        or (str(actor_id) if actor_id else None)
        or "System"
    )


def build_recent_activity_feed(db: Session, events: list, limit: int = 5) -> list[dict]:
    if not events:
        return []
    sliced_events = events[:limit]
    actor_ids = {
        str(event.actor_id)
        for event in sliced_events
        if getattr(event, "actor_id", None) and _is_user_actor(getattr(event, "actor_type", None))
    }
    subscribers: dict[str, Subscriber] = {}
    if actor_ids:
        subscribers = {
            str(subscriber.id): subscriber
            for subscriber in db.query(Subscriber).filter(Subscriber.id.in_(actor_ids)).all()
        }
    activities = []
    for event in sliced_events:
        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(getattr(event, "action", None))
        entity_label = humanize_entity(getattr(event, "entity_type", None), getattr(event, "entity_id", None))
        actor_name = _resolve_actor_name(event, subscribers)
        time_str = format_audit_datetime(getattr(event, "occurred_at", None), "%b %d, %H:%M")
        message = f"{actor_name} {action_label} {entity_label}"
        detail = change_summary or entity_label
        activities.append(
            {
                "message": message,
                "detail": detail,
                "time": time_str,
            }
        )
    return activities


def recent_activity_for_paths(
    db: Session,
    path_prefixes: list[str],
    limit: int = 5,
    fetch_limit: int = 200,
) -> list[dict]:
    events = audit_service.audit_events.list(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=fetch_limit,
        offset=0,
    )
    if not events:
        return []
    filtered = []
    prefixes = tuple(path_prefixes)
    for event in events:
        entity_type = getattr(event, "entity_type", "") or ""
        if entity_type.startswith(prefixes):
            filtered.append(event)
            if len(filtered) >= limit:
                break
    return build_recent_activity_feed(db, filtered, limit=limit)
