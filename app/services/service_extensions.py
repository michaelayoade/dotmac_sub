"""Service extensions: bulk validity compensation for outages.

Pushes next_billing_at forward by N days on every active subscription in
scope. Capped plans keep their calendar-month allowance — validity, not data.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

MAX_EXTENSION_DAYS = 30


def resolve_scope_subscriptions(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: list[str] | None = None,
) -> list[Subscription]:
    """Active subscriptions in scope, with subscriber eagerly loaded."""
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.subscriber))
        .where(Subscription.status == SubscriptionStatus.active)
    )
    if scope_type == ServiceExtensionScope.nas_device:
        if not scope_id:
            raise HTTPException(status_code=400, detail="NAS device is required")
        stmt = stmt.where(
            Subscription.provisioning_nas_device_id == coerce_uuid(scope_id)
        )
    elif scope_type == ServiceExtensionScope.pop_site:
        if not scope_id:
            raise HTTPException(status_code=400, detail="POP site is required")
        stmt = stmt.where(
            Subscription.provisioning_nas_device.has(
                NasDevice.pop_site_id == coerce_uuid(scope_id)
            )
        )
    elif scope_type == ServiceExtensionScope.subscribers:
        ids = [coerce_uuid(s) for s in (subscriber_ids or []) if str(s).strip()]
        if not ids:
            raise HTTPException(
                status_code=400, detail="At least one subscriber is required"
            )
        stmt = stmt.where(Subscription.subscriber_id.in_(ids))
    return list(db.scalars(stmt).all())


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
    # Validates scope inputs (raises on missing scope_id / empty list).
    resolve_scope_subscriptions(db, scope_type, scope_id, subscriber_ids)

    extension = ServiceExtension(
        reason=reason.strip(),
        window_start=window_start,
        window_end=window_end,
        days=days,
        scope_type=scope_type,
        scope_id=coerce_uuid(scope_id) if scope_id else None,
        scope_subscriber_ids=(
            [str(s) for s in subscriber_ids] if subscriber_ids else None
        ),
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
    subscriptions = resolve_scope_subscriptions(
        db,
        extension.scope_type,
        str(extension.scope_id) if extension.scope_id else None,
        extension.scope_subscriber_ids,
    )
    extendable = [s for s in subscriptions if s.next_billing_at is not None]
    return {
        "subscriptions": subscriptions,
        "extendable_count": len(extendable),
        "skipped_count": len(subscriptions) - len(extendable),
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

    subscriptions = resolve_scope_subscriptions(
        db,
        extension.scope_type,
        str(extension.scope_id) if extension.scope_id else None,
        extension.scope_subscriber_ids,
    )

    now = datetime.now(UTC)
    delta = timedelta(days=extension.days)
    applied = 0
    skipped = 0
    for subscription in subscriptions:
        previous = subscription.next_billing_at
        if previous is None:
            skipped += 1
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
