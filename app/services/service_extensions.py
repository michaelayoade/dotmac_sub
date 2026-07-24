"""Canonical exact service-grant intervals for outage compensation.

Each application records an immutable interval and projects next_billing_at to
its end. Capped plans keep their calendar-month allowance — validity, not data.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionAnchorBasis,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_EXTENSION_DAYS = 30
MIN_EXTENSION_DAYS = 1
MAX_ALLOWED_EXTENSION_DAYS = 365
PREVIEW_SAMPLE_LIMIT = 50
APPLY_BATCH_SIZE = 500
# Postgres int4 ceiling: digit strings above this are not legacy customer IDs.
# (e.g. phone numbers) and would overflow the column comparison.
_MAX_INT4 = 2_147_483_647
_OWNER = "financial.service_extensions"
_LIFECYCLE_CONCERN = "service-extension lifecycle and exact grant intervals"
_CREATE_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="create_service_extension",
)
_APPLY_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="apply_service_extension",
)
_CANCEL_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern=_LIFECYCLE_CONCERN,
    name="cancel_service_extension",
)


class ServiceExtensionError(DomainError):
    """Transport-neutral service-extension failure."""


@dataclass(frozen=True, slots=True)
class ServiceExtensionGrantInterval:
    """Exact non-cash service interval decided by the extension owner."""

    starts_at: datetime
    ends_at: datetime
    anchor_basis: ServiceExtensionAnchorBasis


@dataclass(frozen=True, slots=True)
class ServiceExtensionIntervalRow:
    """Admin projection of one proposed or applied extension interval."""

    subscription: Subscription
    previous_next_billing_at: datetime | None
    grant_starts_at: datetime | None
    grant_ends_at: datetime | None
    anchor_basis: ServiceExtensionAnchorBasis | None


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise ServiceExtensionError(
        code=f"access.service_extensions.{suffix}",
        message=message,
        details=details,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _command_context(
    *,
    actor_id: str | None,
    scope: str,
    reason: str,
    idempotency_key: str | None = None,
) -> CommandContext:
    return CommandContext.system(
        actor=actor_id or "system:service_extensions",
        scope=scope,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _record_extension_audit(
    db: Session,
    *,
    action: str,
    extension: ServiceExtension,
    actor_id: str | None,
    metadata: dict[str, object],
) -> None:
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event

    record_audit_event(
        db,
        action=action,
        entity_type="service_extension",
        entity_id=str(extension.id),
        actor_type=AuditActorType.user,
        actor_id=actor_id,
        metadata=metadata,
        defer_until_commit=True,
    )


def resolve_extension_grant_interval(
    *,
    previous_next_billing_at: datetime,
    applied_at: datetime,
    days: int,
) -> ServiceExtensionGrantInterval:
    """Resolve the exact grant interval from authoritative inputs.

    A current or future billing anchor remains additive. A stale anchor cannot
    consume compensation in the past, so the grant begins when it is applied.
    """

    previous = _as_utc(previous_next_billing_at)
    effective_at = _as_utc(applied_at)
    if previous >= effective_at:
        starts_at = previous
        basis = ServiceExtensionAnchorBasis.existing_billing_anchor
    else:
        starts_at = effective_at
        basis = ServiceExtensionAnchorBasis.application_time
    return ServiceExtensionGrantInterval(
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=days),
        anchor_basis=basis,
    )


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def _max_extension_days(db: Session) -> int:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "service_extension_max_days"
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EXTENSION_DAYS
    return max(MIN_EXTENSION_DAYS, min(MAX_ALLOWED_EXTENSION_DAYS, parsed))


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
        _error("blank_customer_identifier", "Customer identifier cannot be blank.")

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
        _error(
            "customer_not_found",
            "Customer was not found.",
            identifier=identifier,
        )

    # 2. Exact account / subscriber number (case-insensitive).
    lowered = identifier.lower()
    for column in (Subscriber.account_number, Subscriber.subscriber_number):
        matches.extend(
            db.scalars(select(Subscriber).where(func.lower(column) == lowered)).all()
        )

    # 3. Imported customer id — int4-bounded so a longer digit string (e.g. an
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
        _error("ambiguous_customer_identifier", ambiguous_detail)
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
        _error("ambiguous_customer_identifier", ambiguous_detail)
    _error(
        "customer_not_found",
        "Customer was not found.",
        identifier=identifier,
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
            _error(
                "invalid_customer_identifier",
                "A customer identifier in the extension scope is invalid.",
                identifier=str(raw_id),
            )
        if subscriber_id in seen:
            continue
        seen.add(subscriber_id)
        resolved.append(subscriber_id)
    return resolved


def _validate_resolved_subscriber_ids(
    db: Session, subscriber_ids: Sequence[str | uuid.UUID] | None
) -> list[uuid.UUID]:
    resolved = _coerce_resolved_subscriber_ids(subscriber_ids)
    if not resolved:
        return []
    existing = set(
        db.scalars(select(Subscriber.id).where(Subscriber.id.in_(resolved))).all()
    )
    missing = [
        str(subscriber_id)
        for subscriber_id in resolved
        if subscriber_id not in existing
    ]
    if missing:
        _error(
            "customer_not_found",
            "A selected customer was not found.",
            identifier=missing[0],
        )
    return resolved


def _subscriber_scope_rows(
    db: Session, subscriber_ids: Sequence[str | uuid.UUID] | None
) -> list[Subscriber]:
    resolved = _coerce_resolved_subscriber_ids(subscriber_ids)
    if not resolved:
        return []
    rows = {
        row.id: row
        for row in db.scalars(
            select(Subscriber).where(Subscriber.id.in_(resolved))
        ).all()
    }
    return [rows[subscriber_id] for subscriber_id in resolved if subscriber_id in rows]


def _scope_filters(
    db: Session,
    scope_type: ServiceExtensionScope,
    scope_id: str | None = None,
    subscriber_ids: Sequence[str | uuid.UUID] | None = None,
    *,
    subscriber_ids_resolved: bool = False,
) -> list:
    # Suspended subscriptions are in scope on purpose: ops reach for an
    # extension precisely when a customer lapsed during an outage window, and
    # silently skipping them left customers extended-on-paper but offline.
    filters: list[ColumnElement[bool]] = [
        Subscription.status.in_(
            (SubscriptionStatus.active, SubscriptionStatus.suspended)
        )
    ]
    if scope_type == ServiceExtensionScope.nas_device:
        if not scope_id:
            _error("missing_scope_id", "NAS device is required.")
        filters.append(Subscription.provisioning_nas_device_id == coerce_uuid(scope_id))
    elif scope_type == ServiceExtensionScope.pop_site:
        if not scope_id:
            _error("missing_scope_id", "POP site is required.")
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
            _error(
                "empty_subscriber_scope",
                "At least one customer is required.",
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


def _validated_days(db: Session, days: int) -> int:
    max_days = _max_extension_days(db)
    if not MIN_EXTENSION_DAYS <= int(days) <= max_days:
        _error(
            "invalid_days",
            f"Days must be between {MIN_EXTENSION_DAYS} and {max_days}.",
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
    subscriber_ids_resolved: bool = False,
    created_by: str | None = None,
) -> ServiceExtension:
    """Create a pending extension. Scope is validated but not applied yet."""
    context = _command_context(
        actor_id=created_by,
        scope=f"service_extensions:{scope_type.value}",
        reason=str(reason or "").strip() or "service extension creation",
    )

    def operation() -> ServiceExtension:
        if not str(reason or "").strip():
            _error("missing_reason", "Reason is required.")
        if window_end <= window_start:
            _error("invalid_window", "Outage end must be after its start.")
        validated_days = _validated_days(db, days)
        resolved_subscriber_ids = None
        if scope_type == ServiceExtensionScope.subscribers:
            resolver = (
                _validate_resolved_subscriber_ids
                if subscriber_ids_resolved
                else resolve_subscriber_identifiers
            )
            resolved_subscriber_ids = [
                str(item) for item in resolver(db, subscriber_ids)
            ]
        # Validate without materializing every network-wide subscription.
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
            days=validated_days,
            scope_type=scope_type,
            scope_id=coerce_uuid(scope_id) if scope_id else None,
            scope_subscriber_ids=resolved_subscriber_ids,
            status=ServiceExtensionStatus.pending,
            created_by=created_by,
        )
        db.add(extension)
        db.flush()
        _record_extension_audit(
            db,
            action="billing.service_extension_created",
            extension=extension,
            actor_id=created_by,
            metadata={
                "days": validated_days,
                "scope_type": scope_type.value,
                "reason": extension.reason,
            },
        )
        db.refresh(extension)
        db.expunge(extension)
        return extension

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=context,
        operation=operation,
    )


def get_extension(db: Session, extension_id: str) -> ServiceExtension:
    try:
        resolved_id = coerce_uuid(extension_id)
    except (TypeError, ValueError):
        _error("invalid_extension_id", "Service extension identifier is invalid.")
    extension = db.get(ServiceExtension, resolved_id)
    if not extension:
        _error("extension_not_found", "Service extension was not found.")
    return extension


def _lock_extension(db: Session, extension_id: str) -> ServiceExtension:
    try:
        resolved_id = coerce_uuid(extension_id)
    except (TypeError, ValueError):
        _error("invalid_extension_id", "Service extension identifier is invalid.")
    extension = db.scalar(
        select(ServiceExtension)
        .where(ServiceExtension.id == resolved_id)
        .with_for_update()
    )
    if extension is None:
        _error("extension_not_found", "Service extension was not found.")
    return extension


def _applied_interval_sample(
    db: Session, extension_id: uuid.UUID
) -> list[ServiceExtensionIntervalRow]:
    rows = db.execute(
        select(ServiceExtensionEntry, Subscription)
        .join(Subscription, Subscription.id == ServiceExtensionEntry.subscription_id)
        .options(joinedload(Subscription.subscriber))
        .where(ServiceExtensionEntry.extension_id == extension_id)
        .order_by(ServiceExtensionEntry.created_at.desc(), ServiceExtensionEntry.id)
        .limit(PREVIEW_SAMPLE_LIMIT)
    ).all()
    return [
        ServiceExtensionIntervalRow(
            subscription=subscription,
            previous_next_billing_at=entry.previous_next_billing_at,
            grant_starts_at=entry.grant_starts_at,
            grant_ends_at=entry.grant_ends_at,
            anchor_basis=entry.anchor_basis,
        )
        for entry, subscription in rows
    ]


def _proposed_interval_row(
    subscription: Subscription, *, applied_at: datetime, days: int
) -> ServiceExtensionIntervalRow:
    previous = subscription.next_billing_at
    interval = (
        resolve_extension_grant_interval(
            previous_next_billing_at=previous,
            applied_at=applied_at,
            days=days,
        )
        if previous is not None
        else None
    )
    return ServiceExtensionIntervalRow(
        subscription=subscription,
        previous_next_billing_at=previous,
        grant_starts_at=interval.starts_at if interval else None,
        grant_ends_at=interval.ends_at if interval else None,
        anchor_basis=interval.anchor_basis if interval else None,
    )


def preview_extension(db: Session, extension: ServiceExtension) -> dict:
    """Return exact applied evidence or a current proposal for pending scope."""
    scope_id = str(extension.scope_id) if extension.scope_id else None
    if extension.status == ServiceExtensionStatus.applied:
        interval_sample = _applied_interval_sample(db, extension.id)
        sample = [row.subscription for row in interval_sample]
        total_count = extension.affected_count + extension.skipped_count
        extendable_count = extension.affected_count
        skipped_count = extension.skipped_count
        previewed_at = extension.applied_at
    else:
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
        previewed_at = _now_utc()
        interval_sample = [
            _proposed_interval_row(
                subscription,
                applied_at=previewed_at,
                days=extension.days,
            )
            for subscription in sample
        ]
        skipped_count = total_count - extendable_count
    return {
        "subscriptions": sample,
        "sample": sample,
        "interval_sample": interval_sample,
        "previewed_at": previewed_at,
        "selected_subscribers": (
            _subscriber_scope_rows(db, extension.scope_subscriber_ids)
            if extension.scope_type == ServiceExtensionScope.subscribers
            else []
        ),
        "total_count": total_count,
        "extendable_count": extendable_count,
        "skipped_count": skipped_count,
    }


def cancel_extension(
    db: Session, extension_id: str, *, actor_id: str | None = None
) -> ServiceExtension:
    context = _command_context(
        actor_id=actor_id,
        scope=f"service_extension:{extension_id}",
        reason="cancel pending service extension",
    )

    def operation() -> ServiceExtension:
        extension = _lock_extension(db, extension_id)
        if extension.status != ServiceExtensionStatus.pending:
            _error(
                "invalid_transition",
                "Only pending extensions can be canceled.",
            )
        extension.status = ServiceExtensionStatus.canceled
        extension.applied_by = actor_id
        db.flush()
        _record_extension_audit(
            db,
            action="billing.service_extension_canceled",
            extension=extension,
            actor_id=actor_id,
            metadata={
                "days": extension.days,
                "scope_type": extension.scope_type.value,
                "reason": extension.reason,
            },
        )
        db.refresh(extension)
        db.expunge(extension)
        return extension

    return execute_owner_command(
        db,
        definition=_CANCEL_COMMAND,
        context=context,
        operation=operation,
    )


def _resume_billing_suspension(
    db: Session, subscription: Subscription, extension: ServiceExtension
) -> bool:
    """Lift billing-driven suspensions so the extension actually restores service.

    Only ``overdue`` (dunning) and ``prepaid`` (balance-lapse) locks are
    resolved; admin, fraud, FUP, and customer-hold locks are deliberately left
    in place — an outage-compensation extension must not override those.
    Returns True if the subscription came back to active.
    """
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import restore_subscription

    for reason in (EnforcementReason.overdue, EnforcementReason.prepaid):
        try:
            restore_subscription(
                db,
                str(subscription.id),
                trigger="admin",
                resolved_by=f"service_extension:{extension.id}",
                reason=reason,
                notes=f"Service extension +{extension.days}d: {extension.reason}",
            )
        except ValueError as exc:
            logger.warning(
                "Extension %s could not resume subscription %s (%s): %s",
                extension.id,
                subscription.id,
                reason.value,
                exc,
            )
        if subscription.status == SubscriptionStatus.active:
            return True
    return False


def _apply_extension(
    db: Session, extension_id: str, *, actor_id: str | None = None
) -> ServiceExtension:
    """Apply a pending extension exactly once: push next_billing_at by N days
    on every in-scope subscription (resuming billing-suspended ones), record an
    entry per subscription, notify each customer, and audit the batch."""
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import record_audit_event
    from app.services.events import emit_event
    from app.services.events.types import EventType

    extension = _lock_extension(db, extension_id)
    if extension.status != ServiceExtensionStatus.pending:
        _error(
            "invalid_transition",
            "Extension has already been applied or canceled.",
        )

    applied_at = _now_utc()
    applied = 0
    skipped = 0
    resumed = 0
    still_suspended: list[str] = []
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
        interval = resolve_extension_grant_interval(
            previous_next_billing_at=previous,
            applied_at=applied_at,
            days=extension.days,
        )
        subscription.next_billing_at = interval.ends_at
        db.add(
            ServiceExtensionEntry(
                extension_id=extension.id,
                subscription_id=subscription.id,
                subscriber_id=subscription.subscriber_id,
                previous_next_billing_at=previous,
                grant_starts_at=interval.starts_at,
                grant_ends_at=interval.ends_at,
                anchor_basis=interval.anchor_basis,
                new_next_billing_at=interval.ends_at,
                created_at=applied_at,
            )
        )
        if subscription.status == SubscriptionStatus.suspended:
            if _resume_billing_suspension(db, subscription, extension):
                resumed += 1
            else:
                still_suspended.append(str(subscription.id))
        emit_event(
            db,
            EventType.service_extended,
            {
                "subscription_id": str(subscription.id),
                "account_id": str(subscription.subscriber_id),
                "days": extension.days,
                "reason": extension.reason,
                "grant_starts_at": interval.starts_at.isoformat(),
                "grant_ends_at": interval.ends_at.isoformat(),
                "anchor_basis": interval.anchor_basis.value,
                "extended_until": interval.ends_at.isoformat(),
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
    extension.applied_at = applied_at

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
            "resumed": resumed,
            "still_suspended": still_suspended,
            "reason": extension.reason,
            "interval_rule": "max(previous_next_billing_at, applied_at) + days",
        },
        defer_until_commit=True,
    )
    db.flush()
    db.refresh(extension)
    db.expunge(extension)
    return extension


def apply_extension(
    db: Session, extension_id: str, *, actor_id: str | None = None
) -> ServiceExtension:
    """Apply one pending extension through its canonical transaction owner."""
    context = _command_context(
        actor_id=actor_id,
        scope=f"service_extension:{extension_id}",
        reason="apply service extension",
        idempotency_key=f"service-extension-apply:{extension_id}",
    )
    return execute_owner_command(
        db,
        definition=_APPLY_COMMAND,
        context=context,
        operation=lambda: _apply_extension(
            db,
            extension_id,
            actor_id=actor_id,
        ),
    )


def extension_shield_reason(db: Session, account_id: str | uuid.UUID) -> str | None:
    """Why billing enforcement should skip this account, or None.

    An applied service extension grants its exact recorded interval regardless
    of arrears. Enforcement uses that same interval as coverage and billing;
    it does not maintain a second clock based on row creation time.
    """
    reasons = bulk_extension_shield_reasons(db, [coerce_uuid(str(account_id))])
    return next(iter(reasons.values()), None)


def bulk_extension_shield_reasons(
    db: Session, account_ids: Sequence[uuid.UUID] | set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Return in-force extension shield reasons for a cohort of accounts."""
    ids = {coerce_uuid(str(account_id)) for account_id in account_ids}
    if not ids:
        return {}
    now = _now_utc()
    rows = db.execute(
        select(
            ServiceExtensionEntry.subscriber_id,
            ServiceExtensionEntry.grant_ends_at,
            ServiceExtension.id,
        )
        .join(
            ServiceExtension, ServiceExtension.id == ServiceExtensionEntry.extension_id
        )
        .where(
            ServiceExtensionEntry.subscriber_id.in_(ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.grant_starts_at.isnot(None),
            ServiceExtensionEntry.grant_starts_at <= now,
            ServiceExtensionEntry.grant_ends_at.isnot(None),
            ServiceExtensionEntry.grant_ends_at > now,
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for subscriber_id, grant_ends_at, extension_id in rows:
        assert grant_ends_at is not None
        reasons.setdefault(
            subscriber_id,
            "service extension "
            f"{extension_id} in force until {grant_ends_at.date().isoformat()}",
        )
    return reasons


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
        "max_days": _max_extension_days(db),
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
