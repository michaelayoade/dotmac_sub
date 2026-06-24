"""Service extensions: bulk validity compensation for outages.

Pushes next_billing_at forward by N days on every active subscription in
scope. Capped plans keep their calendar-month allowance — validity, not data.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.customer_identity_resolution import resolve_customer_identity

logger = logging.getLogger(__name__)

MAX_EXTENSION_DAYS = 30
PREVIEW_SAMPLE_LIMIT = 50
APPLY_BATCH_SIZE = 500
# Postgres int4 ceiling: digit strings above this aren't splynx_customer_ids
# (e.g. phone numbers) and would overflow the column comparison.
_MAX_INT4 = 2_147_483_647


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def _unique_subscribers(rows: list[Subscriber]) -> list[Subscriber]:
    seen: set[uuid.UUID] = set()
    unique: list[Subscriber] = []
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        unique.append(row)
    return unique


def _find_subscriber_by_identifier(db: Session, raw_identifier: str) -> Subscriber:
    identifier = str(raw_identifier or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Blank customer identifier")

    ambiguous_detail = (
        f"Customer identifier is ambiguous: {identifier}. "
        "Use the internal customer UUID."
    )
    matches: list[Subscriber] = []

    # 1. Internal UUID.
    parsed_uuid = _parse_uuid(identifier)
    if parsed_uuid is not None:
        subscriber = db.get(Subscriber, parsed_uuid)
        if subscriber is not None:
            return subscriber
        raise HTTPException(
            status_code=400, detail=f"Could not find customer: {identifier}"
        )

    # 2. Exact account / subscriber number (case-insensitive).
    lowered = identifier.lower()
    for column in (Subscriber.account_number, Subscriber.subscriber_number):
        matches.extend(
            db.scalars(select(Subscriber).where(func.lower(column) == lowered)).all()
        )

    # 3. Splynx customer id — int4-bounded so a longer digit string (e.g. an
    #    11-digit phone number) doesn't overflow the int4 column on Postgres.
    if identifier.isdigit() and int(identifier) <= _MAX_INT4:
        matches.extend(
            db.scalars(
                select(Subscriber).where(
                    Subscriber.splynx_customer_id == int(identifier)
                )
            ).all()
        )

    matches = _unique_subscribers(matches)
    if len(matches) > 1:
        raise HTTPException(status_code=400, detail=ambiguous_detail)
    if len(matches) == 1:
        return matches[0]

    # 4. Email / phone via the indexed customer-identity resolver (auto-detects
    #    type, queries customer_identity_index — no full table scan). A shared
    #    contact email (non-unique post-decoupling) resolves as ambiguous.
    resolution = resolve_customer_identity(db, identifier)
    if (
        resolution.matched
        and not resolution.ambiguous
        and resolution.subscriber_id is not None
    ):
        subscriber = db.get(Subscriber, resolution.subscriber_id)
        if subscriber is not None:
            matches.append(subscriber)

    matches = _unique_subscribers(matches)
    if len(matches) == 1:
        return matches[0]
    # No exact match: an email/phone that resolved to several customers is
    # ambiguous; anything else is simply unknown.
    if resolution.ambiguous:
        raise HTTPException(status_code=400, detail=ambiguous_detail)
    raise HTTPException(
        status_code=400, detail=f"Could not find customer: {identifier}"
    )


def resolve_subscriber_identifiers(
    db: Session, subscriber_ids: list[str] | None
) -> list[uuid.UUID]:
    resolved: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw_identifier in subscriber_ids or []:
        subscriber = _find_subscriber_by_identifier(db, raw_identifier)
        if subscriber.id in seen:
            continue
        seen.add(subscriber.id)
        resolved.append(subscriber.id)
    return resolved


def _coerce_resolved_subscriber_ids(
    subscriber_ids: Sequence[str | uuid.UUID] | None,
) -> list[uuid.UUID]:
    resolved: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for raw_id in subscriber_ids or []:
        subscriber_id = _parse_uuid(str(raw_id))
        if subscriber_id is None:
            raise HTTPException(
                status_code=400, detail=f"Invalid subscriber id in scope: {raw_id}"
            )
        if subscriber_id in seen:
            continue
        seen.add(subscriber_id)
        resolved.append(subscriber_id)
    return resolved


def _scope_filters(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> list:
    filters = [Subscription.status == SubscriptionStatus.active]
    if scope_type == ServiceExtensionScope.nas_device:
        if not scope_id:
            raise HTTPException(status_code=400, detail="NAS device is required")
        filters.append(Subscription.provisioning_nas_device_id == coerce_uuid(scope_id))
    elif scope_type == ServiceExtensionScope.pop_site:
        if not scope_id:
            raise HTTPException(status_code=400, detail="POP site is required")
        filters.append(
            Subscription.provisioning_nas_device.has(
                NasDevice.pop_site_id == coerce_uuid(scope_id)
            )
        )
    elif scope_type == ServiceExtensionScope.subscribers:
        ids = (
            _coerce_resolved_subscriber_ids(subscriber_ids)
            if subscriber_ids_resolved
            else resolve_subscriber_identifiers(
                db, [str(s) for s in (subscriber_ids or [])]
            )
        )
        if not ids:
            raise HTTPException(
                status_code=400, detail="At least one subscriber is required"
            )
        filters.append(Subscription.subscriber_id.in_(ids))
    return filters


def _scope_subscription_counts(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> tuple[int, int]:
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    total = db.scalar(select(func.count(Subscription.id)).where(*filters)) or 0
    extendable = (
        db.scalar(
            select(func.count(Subscription.id)).where(
                *filters, Subscription.next_billing_at.is_not(None)
            )
        )
        or 0
    )
    return int(total), int(extendable)


def _scope_subscription_sample(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    limit: int = PREVIEW_SAMPLE_LIMIT,
    subscriber_ids_resolved: bool = False,
) -> list[Subscription]:
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(*filters)
        .order_by(Subscription.created_at.desc(), Subscription.id)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def resolve_scope_subscriptions(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> list[Subscription]:
    """Active subscriptions in scope, with subscriber eagerly loaded."""
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(*filters)
    )
    return list(db.scalars(stmt).all())


def _iter_scope_subscriptions(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    batch_size: int = APPLY_BATCH_SIZE,
    subscriber_ids_resolved: bool = False,
):
    filters = _scope_filters(
        db,
        scope_type,
        scope_id,
        subscriber_ids,
        subscriber_ids_resolved=subscriber_ids_resolved,
    )
    offset = 0
    while True:
        ids = list(
            db.scalars(
                select(Subscription.id)
                .where(*filters)
                .order_by(Subscription.id)
                .limit(batch_size)
                .offset(offset)
            ).all()
        )
        if not ids:
            break
        subscriptions = list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(ids))
                .order_by(Subscription.id)
            ).all()
        )
        yield from subscriptions
        offset += len(ids)


def _validated_days(days: int) -> int:
    if not 1 <= int(days) <= MAX_EXTENSION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Days must be between 1 and {MAX_EXTENSION_DAYS}",
        )
    return int(days)


def create_extension(
    db: Session,
    *,
    reason: str,
    window_start: datetime,
    window_end: datetime,
    days: int,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: list[str] | None = None,
    created_by: str | None = None,
) -> ServiceExtension:
    """Create a pending extension. Scope is validated but not applied yet."""
    if not str(reason or "").strip():
        raise HTTPException(status_code=400, detail="Reason is required")
    if window_end <= window_start:
        raise HTTPException(
            status_code=400, detail="Outage end must be after its start"
        )
    days = _validated_days(days)
    resolved_subscriber_ids = (
        [str(item) for item in resolve_subscriber_identifiers(db, subscriber_ids)]
        if scope_type == ServiceExtensionScope.subscribers
        else None
    )
    # Validates scope inputs (raises on missing scope_id / empty list) without
    # materializing every active subscription for network-wide extensions.
    _scope_subscription_counts(
        db,
        scope_type,
        scope_id,
        resolved_subscriber_ids,
        subscriber_ids_resolved=scope_type == ServiceExtensionScope.subscribers,
    )

    extension = ServiceExtension(
        reason=reason.strip(),
        window_start=window_start,
        window_end=window_end,
        days=days,
        scope_type=scope_type,
        scope_id=coerce_uuid(scope_id) if scope_id else None,
        scope_subscriber_ids=resolved_subscriber_ids,
        status=ServiceExtensionStatus.pending,
        created_by=created_by,
    )
    db.add(extension)
    db.commit()
    db.refresh(extension)
    return extension


def get_extension(db: Session, extension_id: str) -> ServiceExtension:
    extension = db.get(ServiceExtension, coerce_uuid(extension_id))
    if not extension:
        raise HTTPException(status_code=404, detail="Service extension not found")
    return extension


def preview_extension(db: Session, extension: ServiceExtension) -> dict:
    """Affected subscriptions as of now (recomputed at apply time too)."""
    scope_id = str(extension.scope_id) if extension.scope_id else None
    total_count, extendable_count = _scope_subscription_counts(
        db,
        extension.scope_type,
        scope_id,
        extension.scope_subscriber_ids,
        subscriber_ids_resolved=extension.scope_type
        == ServiceExtensionScope.subscribers,
    )
    sample = _scope_subscription_sample(
        db,
        extension.scope_type,
        scope_id,
        extension.scope_subscriber_ids,
        subscriber_ids_resolved=extension.scope_type
        == ServiceExtensionScope.subscribers,
    )
    return {
        "subscriptions": sample,
        "sample": sample,
        "total_count": total_count,
        "extendable_count": extendable_count,
        "skipped_count": total_count - extendable_count,
    }


def cancel_extension(
    db: Session, extension_id: str, *, actor_id: str | None = None
) -> ServiceExtension:
    extension = get_extension(db, extension_id)
    if extension.status != ServiceExtensionStatus.pending:
        raise HTTPException(
            status_code=409, detail="Only pending extensions can be canceled"
        )
    extension.status = ServiceExtensionStatus.canceled
    extension.applied_by = actor_id
    db.commit()
    return extension


def apply_extension(
    db: Session, extension_id: str, *, actor_id: str | None = None
) -> ServiceExtension:
    """Apply a pending extension exactly once: push next_billing_at by N days
    on every active in-scope subscription, record an entry per subscription,
    notify each customer, and audit the batch."""
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event
    from app.services.events import emit_event
    from app.services.events.types import EventType

    extension = get_extension(db, extension_id)
    if extension.status != ServiceExtensionStatus.pending:
        raise HTTPException(
            status_code=409, detail="Extension has already been applied or canceled"
        )

    now = datetime.now(UTC)
    delta = timedelta(days=extension.days)
    applied = 0
    skipped = 0
    processed = 0
    for subscription in _iter_scope_subscriptions(
        db,
        extension.scope_type,
        str(extension.scope_id) if extension.scope_id else None,
        extension.scope_subscriber_ids,
        subscriber_ids_resolved=extension.scope_type
        == ServiceExtensionScope.subscribers,
    ):
        previous = subscription.next_billing_at
        if previous is None:
            skipped += 1
            processed += 1
            if processed % APPLY_BATCH_SIZE == 0:
                db.flush()
            continue
        subscription.next_billing_at = previous + delta
        db.add(
            ServiceExtensionEntry(
                extension_id=extension.id,
                subscription_id=subscription.id,
                subscriber_id=subscription.subscriber_id,
                previous_next_billing_at=previous,
                new_next_billing_at=subscription.next_billing_at,
            )
        )
        emit_event(
            db,
            EventType.service_extended,
            {
                "subscription_id": str(subscription.id),
                "account_id": str(subscription.subscriber_id),
                "days": extension.days,
                "reason": extension.reason,
                "extended_until": subscription.next_billing_at.isoformat(),
            },
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            account_id=subscription.subscriber_id,
        )
        applied += 1
        processed += 1
        if processed % APPLY_BATCH_SIZE == 0:
            db.flush()

    extension.status = ServiceExtensionStatus.applied
    extension.affected_count = applied
    extension.skipped_count = skipped
    extension.applied_by = actor_id
    extension.applied_at = now

    record_audit_event(
        db,
        action="billing.service_extension_applied",
        entity_type="service_extension",
        entity_id=str(extension.id),
        actor_type=AuditActorType.user,
        actor_id=actor_id,
        metadata={
            "days": extension.days,
            "scope_type": extension.scope_type.value,
            "affected": applied,
            "skipped": skipped,
            "reason": extension.reason,
        },
        defer_until_commit=True,
    )
    db.commit()
    db.refresh(extension)
    return extension


def scope_options(db: Session) -> dict:
    """POP sites and NAS devices for the extension form's scope selectors."""
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import PopSite

    return {
        "pop_sites": list(db.scalars(select(PopSite).order_by(PopSite.name)).all()),
        "nas_devices": list(
            db.scalars(select(NasDevice).order_by(NasDevice.name)).all()
        ),
        "scope_types": [item.value for item in ServiceExtensionScope],
        "max_days": MAX_EXTENSION_DAYS,
    }


def list_extensions(
    db: Session, *, limit: int = 50, offset: int = 0
) -> list[ServiceExtension]:
    return list(
        db.scalars(
            select(ServiceExtension)
            .order_by(ServiceExtension.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
