from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, event, func, not_, or_, select
from sqlalchemy.orm import Session, object_session

from app.models.splynx_mapping import SplynxEntityType, SplynxIdMapping
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.splynx_mapping import splynx_mapping

logger = logging.getLogger(__name__)


def customer_id_expression(model=Subscriber):
    """SQL expression for the legacy external BSS customer identifier column."""
    return (
        select(SplynxIdMapping.splynx_id)
        .where(SplynxIdMapping.entity_type == SplynxEntityType.customer)
        .where(SplynxIdMapping.dotmac_id == model.id)
        .limit(1)
        .scalar_subquery()
    )


def get_customer_id(subscriber: Subscriber | None) -> int | None:
    """Return the normalized legacy external BSS customer identifier."""
    if subscriber is None:
        return None
    session = object_session(subscriber)
    subscriber_id = getattr(subscriber, "id", None)
    if session is not None and subscriber_id is not None:
        mapped_value = splynx_mapping.lookup_by_dotmac(
            session,
            SplynxEntityType.customer,
            subscriber_id,
        )
        if mapped_value is not None:
            return int(mapped_value)
    value = getattr(subscriber, "_legacy_bss_customer_id", None)
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        return None
    except (TypeError, ValueError):
        return None


def set_customer_id(subscriber: Subscriber, value: int | str | None) -> None:
    """Set the legacy external BSS customer identifier with normalized semantics."""
    normalized = None
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value != "":
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Legacy external BSS customer id must be an integer"
            ) from exc
    if getattr(subscriber, "id", None) is None:
        subscriber.id = uuid.uuid4()
    object.__setattr__(subscriber, "_legacy_bss_customer_id", normalized)
    session = object_session(subscriber)
    subscriber_id = getattr(subscriber, "id", None)
    if session is None or subscriber_id is None:
        return
    if normalized is None:
        splynx_mapping.delete_by_dotmac(
            session, SplynxEntityType.customer, subscriber_id
        )
        return
    splynx_mapping.register_or_update(
        session,
        SplynxEntityType.customer,
        normalized,
        subscriber_id,
        metadata={"source": "legacy_bss_adapter"},
    )


def _sync_pending_customer_mapping(session: Session, subscriber: Subscriber) -> None:
    subscriber_id = getattr(subscriber, "id", None)
    if subscriber_id is None:
        return
    normalized = getattr(subscriber, "_legacy_bss_customer_id", None)
    if normalized is None:
        splynx_mapping.delete_by_dotmac(
            session,
            SplynxEntityType.customer,
            subscriber_id,
            flush=False,
        )
        return
    splynx_mapping.register_or_update(
        session,
        SplynxEntityType.customer,
        int(normalized),
        subscriber_id,
        metadata={"source": "legacy_bss_adapter"},
        flush=False,
    )


@event.listens_for(Session, "before_flush")
def _sync_customer_mappings_before_flush(
    session: Session,
    _flush_context,
    _instances,
) -> None:
    for obj in session.new.union(session.dirty):
        if isinstance(obj, Subscriber) and hasattr(obj, "_legacy_bss_customer_id"):
            _sync_pending_customer_mapping(session, obj)


def _metadata_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, int):
        return value == 1
    return False


def _metadata_text_clause(key: str):
    return func.lower(
        func.trim(func.coalesce(Subscriber.metadata_[key].as_string(), ""))
    )


def _coerce_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def metadata_datetime(metadata: dict | None, key: str) -> datetime | None:
    if not metadata:
        return None
    value = metadata.get(key)
    if isinstance(value, datetime):
        return _coerce_utc_datetime(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Could not parse metadata datetime key=%s value=%r", key, value)
        return None
    return _coerce_utc_datetime(parsed)


def is_deleted_import(subscriber: Subscriber) -> bool:
    """Return whether a subscriber represents a soft-deleted legacy-BSS import."""
    metadata = subscriber.metadata_ or {}
    if _metadata_flag(metadata.get("splynx_deleted")):
        return True
    if not get_customer_id(subscriber):
        return False
    if subscriber.is_active:
        return False
    if subscriber.status != SubscriberStatus.canceled:
        return False
    raw_status = str(metadata.get("splynx_status") or "").strip().lower()
    return raw_status not in {"", "deleted", "canceled"}


def deleted_import_clause():
    """SQL clause matching soft-deleted legacy-BSS imported subscribers."""
    splynx_deleted = _metadata_text_clause("splynx_deleted")
    splynx_status = _metadata_text_clause("splynx_status")
    return or_(
        splynx_deleted.in_(("1", "true", "yes", "on")),
        and_(
            customer_id_expression().is_not(None),
            Subscriber.is_active.is_(False),
            Subscriber.status == SubscriberStatus.canceled,
            not_(splynx_status.in_(("", "deleted", "canceled"))),
        ),
    )


def get_effective_created_at(subscriber: Subscriber) -> datetime | None:
    metadata = subscriber.metadata_ or {}
    source_created = metadata_datetime(metadata, "splynx_date_add")
    if source_created is not None:
        return source_created
    if get_customer_id(subscriber) and subscriber.account_start_date:
        return _coerce_utc_datetime(subscriber.account_start_date)
    return _coerce_utc_datetime(subscriber.created_at)


def get_effective_updated_at(subscriber: Subscriber) -> datetime | None:
    metadata = subscriber.metadata_ or {}
    source_updated = metadata_datetime(metadata, "splynx_last_update")
    if source_updated is not None:
        return source_updated
    return _coerce_utc_datetime(subscriber.updated_at)
