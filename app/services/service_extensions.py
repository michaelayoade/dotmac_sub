"""Service extensions: bulk validity compensation for outages.

Pushes next_billing_at forward by N days on every active subscription in
scope. Capped plans keep their calendar-month allowance — validity, not data.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditActorType
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.service_extension import (
    ServiceExtension,
    ServiceExtensionEntry,
    ServiceExtensionScope,
    ServiceExtensionStatus,
)
from app.models.subscriber import Subscriber
from app.services import settings_spec
from app.services.common import coerce_uuid
from app.services.customer_identity_resolution import resolve_customer_identity
from app.services.domain_errors import DomainError
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

DEFAULT_MAX_EXTENSION_DAYS = 30
MIN_EXTENSION_DAYS = 1
MAX_ALLOWED_EXTENSION_DAYS = 365
PREVIEW_SAMPLE_LIMIT = 50
APPLY_BATCH_SIZE = 500
# Postgres int4 ceiling: digit strings above this are not legacy customer IDs.
# (e.g. phone numbers) and would overflow the column comparison.
_MAX_INT4 = 2_147_483_647
_EXTENSION_ID_NAMESPACE = uuid.UUID("bf7a52db-180b-45b5-89c1-9aa235dd638b")

CREATE_SCOPE = "billing:extension:create"
APPLY_SCOPE = "billing:extension:apply"
CANCEL_SCOPE = "billing:extension:apply"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension aggregate lifecycle",
    name="create_service_extension",
)
_APPLY_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension aggregate lifecycle",
    name="apply_service_extension",
)
_CANCEL_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="service-extension aggregate lifecycle",
    name="cancel_service_extension",
)
_REPAIR_ANCHOR_COMMAND = OwnerCommandDefinition(
    owner="financial.service_extensions",
    concern="extension-caused subscription billing-anchor projection",
    name="repair_service_extension_anchor_projection",
)


@dataclass(frozen=True, slots=True)
class CreateServiceExtensionCommand:
    context: CommandContext
    reason: str
    window_start: datetime
    window_end: datetime
    days: int
    scope_type: ServiceExtensionScope
    scope_id: uuid.UUID | None = None
    subscriber_identifiers: tuple[str, ...] = ()
    subscriber_ids_resolved: bool = False


@dataclass(frozen=True, slots=True)
class ApplyServiceExtensionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CancelServiceExtensionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RepairServiceExtensionAnchorProjectionCommand:
    context: CommandContext
    extension_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class CreateServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ApplyServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    resumed_count: int
    still_suspended_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class CancelServiceExtensionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    affected_count: int
    skipped_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class RepairServiceExtensionAnchorProjectionOutcome:
    extension_id: uuid.UUID
    status: ServiceExtensionStatus
    inspected_count: int
    repaired_count: int
    command_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreviewSubscription:
    id: uuid.UUID
    subscriber_id: uuid.UUID
    subscriber_label: str
    login: str | None
    next_billing_at: datetime | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreviewSubscriber:
    id: uuid.UUID
    label: str
    account_number: str | None
    email: str | None


@dataclass(frozen=True, slots=True)
class ServiceExtensionPreview:
    subscriptions: tuple[ServiceExtensionPreviewSubscription, ...]
    selected_subscribers: tuple[ServiceExtensionPreviewSubscriber, ...]
    total_count: int
    extendable_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionScopeChoice:
    id: uuid.UUID
    label: str


@dataclass(frozen=True, slots=True)
class ServiceExtensionScopeOptions:
    pop_sites: tuple[ServiceExtensionScopeChoice, ...]
    nas_devices: tuple[ServiceExtensionScopeChoice, ...]
    scope_types: tuple[ServiceExtensionScope, ...]
    max_days: int


@dataclass(frozen=True, slots=True)
class ServiceExtensionTransitionEligibility:
    can_apply: bool
    can_cancel: bool


class ServiceExtensionError(DomainError):
    """Transport-neutral service-extension failure."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise ServiceExtensionError(
        code=f"financial.service_extensions.{suffix}",
        message=message,
        details=details,
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
                .with_for_update()
            ).all()
        )
        if not ids:
            break
        subscriptions = list(
            db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(ids))
                .order_by(Subscription.id)
                .with_for_update()
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


def _require_command_context(
    context: CommandContext,
    *,
    expected_scope: str,
) -> str:
    if context.scope != expected_scope:
        _error(
            "invalid_scope",
            "The service-extension command scope is invalid.",
            expected_scope=expected_scope,
        )
    raw_key = str(context.idempotency_key or "").strip()
    if not raw_key:
        _error(
            "missing_idempotency_key",
            "A service-extension idempotency key is required.",
        )
    try:
        return str(uuid.UUID(raw_key))
    except ValueError:
        _error(
            "invalid_idempotency_key",
            "The service-extension idempotency key must be a UUID.",
        )


def _utc_datetime(db: Session, value: datetime) -> datetime:
    if value.tzinfo is None:
        from app.services.display_format import display_timezone

        value = value.replace(tzinfo=display_timezone(db))
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _extension_id(idempotency_key: str) -> uuid.UUID:
    return uuid.uuid5(_EXTENSION_ID_NAMESPACE, idempotency_key)


def _lock_create_key(db: Session, extension_id: uuid.UUID) -> None:
    """Serialize production create replays before the deterministic PK insert."""

    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(extension_id.bytes[:8], byteorder="big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))


def _candidate_extension_id(idempotency_key: str | None) -> uuid.UUID:
    raw_key = str(idempotency_key or "").strip()
    try:
        raw_key = str(uuid.UUID(raw_key))
    except ValueError:
        pass
    return _extension_id(raw_key)


def _transition_id(extension_id: uuid.UUID, action: str) -> uuid.UUID:
    return uuid.uuid5(_EXTENSION_ID_NAMESPACE, f"{extension_id}:{action}")


def transition_idempotency_key(extension_id: uuid.UUID, action: str) -> str:
    if action not in {"apply", "cancel"}:
        _error(
            "invalid_transition_action",
            "The service-extension transition action is invalid.",
            action=action,
        )
    return str(_transition_id(extension_id, action))


def transition_eligibility(
    status: ServiceExtensionStatus,
) -> ServiceExtensionTransitionEligibility:
    pending = status == ServiceExtensionStatus.pending
    return ServiceExtensionTransitionEligibility(
        can_apply=pending,
        can_cancel=pending,
    )


def _actor(context: CommandContext) -> tuple[AuditActorType, str]:
    prefix, separator, identifier = context.actor.partition(":")
    actor_id = identifier if separator and identifier else context.actor
    if prefix == "api_key":
        return AuditActorType.api_key, actor_id
    if prefix == "user":
        return AuditActorType.user, actor_id
    if prefix == "service":
        return AuditActorType.service, actor_id
    return AuditActorType.system, actor_id


def _create_fingerprint(
    *,
    reason: str,
    window_start: datetime,
    window_end: datetime,
    days: int,
    scope_type: ServiceExtensionScope,
    scope_id: uuid.UUID | None,
    subscriber_ids: Sequence[uuid.UUID],
) -> str:
    payload = {
        "reason": reason,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "days": days,
        "scope_type": scope_type.value,
        "scope_id": str(scope_id) if scope_id else None,
        "subscriber_ids": sorted(str(item) for item in subscriber_ids),
    }
    return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _stored_command_id(
    stored: uuid.UUID | None,
    *,
    extension_id: uuid.UUID,
    action: str,
) -> uuid.UUID:
    return stored or _transition_id(extension_id, action)


def _create_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> CreateServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.create_command_id,
        extension_id=extension.id,
        action="create",
    )
    return CreateServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        command_id=command_id,
        correlation_id=extension.create_correlation_id or command_id,
        replayed=replayed,
    )


def _apply_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> ApplyServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.apply_command_id,
        extension_id=extension.id,
        action="apply",
    )
    return ApplyServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        resumed_count=int(extension.resumed_count),
        still_suspended_count=int(extension.still_suspended_count),
        command_id=command_id,
        correlation_id=extension.apply_correlation_id or command_id,
        replayed=replayed,
    )


def _cancel_outcome(
    extension: ServiceExtension,
    *,
    replayed: bool,
) -> CancelServiceExtensionOutcome:
    command_id = _stored_command_id(
        extension.cancel_command_id,
        extension_id=extension.id,
        action="cancel",
    )
    return CancelServiceExtensionOutcome(
        extension_id=extension.id,
        status=extension.status,
        affected_count=int(extension.affected_count),
        skipped_count=int(extension.skipped_count),
        command_id=command_id,
        correlation_id=extension.cancel_correlation_id or command_id,
        replayed=replayed,
    )


def _assert_create_replay(
    extension: ServiceExtension,
    *,
    fingerprint: str,
) -> CreateServiceExtensionOutcome:
    if extension.create_fingerprint_sha256 != fingerprint:
        _error(
            "idempotency_conflict",
            "The idempotency key was already used with different extension inputs.",
            extension_id=str(extension.id),
        )
    return _create_outcome(extension, replayed=True)


def _stage_lifecycle_evidence(
    db: Session,
    *,
    extension: ServiceExtension,
    context: CommandContext,
    action: str,
    event_type: EventType,
    occurred_at: datetime,
    previous_status: ServiceExtensionStatus | None,
    idempotency_key_sha256: str,
    command_fingerprint_sha256: str | None = None,
) -> None:
    from app.services.audit_adapter import stage_audit_event
    from app.services.audit_helpers import resolve_actor_label_from_db
    from app.services.events import emit_event

    actor_type, actor_id = _actor(context)
    actor_label = resolve_actor_label_from_db(db, actor_id, actor_type)
    if not actor_label:
        actor_label = {
            AuditActorType.api_key: "Integration",
            AuditActorType.service: "Automated service",
            AuditActorType.system: "System",
            AuditActorType.user: "Staff member",
        }[actor_type]
    metadata: dict[str, object] = {
        "schema_version": 1,
        "command_id": str(context.command_id),
        "correlation_id": str(context.correlation_id),
        "idempotency_key_sha256": idempotency_key_sha256,
        "days": int(extension.days),
        "scope_type": extension.scope_type.value,
        "resulting_status": extension.status.value,
        "affected": int(extension.affected_count),
        "skipped": int(extension.skipped_count),
        "resumed": int(extension.resumed_count),
        "still_suspended": int(extension.still_suspended_count),
    }
    if previous_status is not None:
        metadata["previous_status"] = previous_status.value
    if command_fingerprint_sha256 is not None:
        metadata["command_fingerprint_sha256"] = command_fingerprint_sha256
    stage_audit_event(
        db,
        action=action,
        entity_type="service_extension",
        entity_id=str(extension.id),
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        request_id=str(context.correlation_id),
        occurred_at=occurred_at,
        metadata=metadata,
    )
    emit_event(
        db,
        event_type,
        {
            "schema_version": 1,
            "extension_id": str(extension.id),
            **metadata,
        },
        actor=context.actor,
    )


def create_service_extension(
    db: Session,
    command: CreateServiceExtensionCommand,
) -> CreateServiceExtensionOutcome:
    """Create or replay one pending extension in an owner-managed transaction."""

    replay_id = _candidate_extension_id(command.context.idempotency_key)

    def operation() -> CreateServiceExtensionOutcome:
        idempotency_key = _require_command_context(
            command.context,
            expected_scope=CREATE_SCOPE,
        )
        _lock_create_key(db, replay_id)
        reason = str(command.reason or "").strip()
        if not reason:
            _error("missing_reason", "Reason is required.")
        window_start = _utc_datetime(db, command.window_start)
        window_end = _utc_datetime(db, command.window_end)
        if window_end <= window_start:
            _error("invalid_window", "Outage end must be after its start.")
        days = _validated_days(db, command.days)

        resolved_subscriber_ids: list[uuid.UUID] = []
        if command.scope_type == ServiceExtensionScope.subscribers:
            resolver = (
                _validate_resolved_subscriber_ids
                if command.subscriber_ids_resolved
                else resolve_subscriber_identifiers
            )
            resolved_subscriber_ids = resolver(
                db,
                list(command.subscriber_identifiers),
            )
        scope_id = command.scope_id
        _scope_subscription_counts(
            db,
            command.scope_type,
            str(scope_id) if scope_id else None,
            resolved_subscriber_ids,
            subscriber_ids_resolved=(
                command.scope_type == ServiceExtensionScope.subscribers
            ),
        )
        fingerprint = _create_fingerprint(
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            days=days,
            scope_type=command.scope_type,
            scope_id=scope_id,
            subscriber_ids=resolved_subscriber_ids,
        )
        existing = db.get(ServiceExtension, replay_id)
        if existing is not None:
            return _assert_create_replay(existing, fingerprint=fingerprint)

        now = datetime.now(UTC)
        extension = ServiceExtension(
            id=replay_id,
            reason=reason,
            window_start=window_start,
            window_end=window_end,
            days=days,
            scope_type=command.scope_type,
            scope_id=scope_id,
            scope_subscriber_ids=[str(item) for item in resolved_subscriber_ids]
            or None,
            status=ServiceExtensionStatus.pending,
            created_by=_actor(command.context)[1],
            created_at=now,
            create_idempotency_key_sha256=_sha256(idempotency_key),
            create_fingerprint_sha256=fingerprint,
            create_command_id=command.context.command_id,
            create_correlation_id=command.context.correlation_id,
        )
        db.add(extension)
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_created",
            event_type=EventType.service_extension_created,
            occurred_at=now,
            previous_status=None,
            idempotency_key_sha256=_sha256(idempotency_key),
            command_fingerprint_sha256=fingerprint,
        )
        return _create_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_CREATE_COMMAND,
        context=command.context,
        operation=operation,
    )


def get_extension(
    db: Session,
    extension_id: str | uuid.UUID,
    *,
    lock: bool = False,
) -> ServiceExtension:
    try:
        resolved_id = coerce_uuid(str(extension_id))
    except (TypeError, ValueError):
        _error("invalid_extension_id", "Service extension identifier is invalid.")
    statement = select(ServiceExtension).where(ServiceExtension.id == resolved_id)
    if lock:
        statement = statement.with_for_update()
    extension = db.scalar(statement)
    if not extension:
        _error("extension_not_found", "Service extension was not found.")
    return extension


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if subscriber is None:
        return "Unknown customer"
    return (
        str(subscriber.display_name or "").strip()
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or "Unknown customer"
    )


def preview_extension(
    db: Session,
    extension: ServiceExtension,
) -> ServiceExtensionPreview:
    """Read-only, typed impact preview for the exact extension scope."""
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
    selected = (
        _subscriber_scope_rows(db, extension.scope_subscriber_ids)
        if extension.scope_type == ServiceExtensionScope.subscribers
        else []
    )
    return ServiceExtensionPreview(
        subscriptions=tuple(
            ServiceExtensionPreviewSubscription(
                id=item.id,
                subscriber_id=item.subscriber_id,
                subscriber_label=_subscriber_label(item.subscriber),
                login=item.login,
                next_billing_at=item.next_billing_at,
            )
            for item in sample
        ),
        selected_subscribers=tuple(
            ServiceExtensionPreviewSubscriber(
                id=item.id,
                label=_subscriber_label(item),
                account_number=item.account_number or item.subscriber_number,
                email=item.email,
            )
            for item in selected
        ),
        total_count=total_count,
        extendable_count=extendable_count,
        skipped_count=total_count - extendable_count,
    )


def cancel_service_extension(
    db: Session,
    command: CancelServiceExtensionCommand,
) -> CancelServiceExtensionOutcome:
    """Cancel or replay one pending extension without altering apply evidence."""

    def operation() -> CancelServiceExtensionOutcome:
        idempotency_key = _require_command_context(
            command.context,
            expected_scope=CANCEL_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status == ServiceExtensionStatus.canceled:
            return _cancel_outcome(extension, replayed=True)
        if extension.status == ServiceExtensionStatus.applied:
            _error(
                "transition_conflict",
                "An applied service extension cannot be canceled.",
                current_status=extension.status.value,
            )
        previous_status = extension.status
        now = datetime.now(UTC)
        extension.status = ServiceExtensionStatus.canceled
        extension.canceled_by = _actor(command.context)[1]
        extension.canceled_at = now
        extension.cancel_idempotency_key_sha256 = _sha256(idempotency_key)
        extension.cancel_command_id = command.context.command_id
        extension.cancel_correlation_id = command.context.correlation_id
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_canceled",
            event_type=EventType.service_extension_canceled,
            occurred_at=now,
            previous_status=previous_status,
            idempotency_key_sha256=_sha256(idempotency_key),
        )
        return _cancel_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_CANCEL_COMMAND,
        context=command.context,
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
        except ValueError:
            _error(
                "access_restoration_failed",
                "The extension could not safely request access restoration.",
                subscription_id=str(subscription.id),
                reason=reason.value,
            )
        if subscription.status == SubscriptionStatus.active:
            return True
    return False


def apply_service_extension(
    db: Session,
    command: ApplyServiceExtensionCommand,
) -> ApplyServiceExtensionOutcome:
    """Apply or replay the locked extension transition atomically."""

    def operation() -> ApplyServiceExtensionOutcome:
        from app.services.events import emit_event

        idempotency_key = _require_command_context(
            command.context,
            expected_scope=APPLY_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status == ServiceExtensionStatus.applied:
            return _apply_outcome(extension, replayed=True)
        if extension.status == ServiceExtensionStatus.canceled:
            _error(
                "transition_conflict",
                "A canceled service extension cannot be applied.",
                current_status=extension.status.value,
            )

        previous_status = extension.status
        now = datetime.now(UTC)
        delta = timedelta(days=extension.days)
        applied = 0
        skipped = 0
        resumed = 0
        still_suspended = 0
        processed = 0
        for subscription in _iter_scope_subscriptions(
            db,
            extension.scope_type,
            str(extension.scope_id) if extension.scope_id else None,
            extension.scope_subscriber_ids,
            subscriber_ids_resolved=(
                extension.scope_type == ServiceExtensionScope.subscribers
            ),
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
                    created_at=now,
                )
            )
            if subscription.status == SubscriptionStatus.suspended:
                if _resume_billing_suspension(db, subscription, extension):
                    resumed += 1
                else:
                    still_suspended += 1
            emit_event(
                db,
                EventType.service_extended,
                {
                    "schema_version": 1,
                    "extension_id": str(extension.id),
                    "subscription_id": str(subscription.id),
                    "account_id": str(subscription.subscriber_id),
                    "days": extension.days,
                    "reason": extension.reason,
                    "extended_until": subscription.next_billing_at.isoformat(),
                    "command_id": str(command.context.command_id),
                    "correlation_id": str(command.context.correlation_id),
                },
                actor=command.context.actor,
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
        extension.resumed_count = resumed
        extension.still_suspended_count = still_suspended
        extension.applied_by = _actor(command.context)[1]
        extension.applied_at = now
        extension.apply_idempotency_key_sha256 = _sha256(idempotency_key)
        extension.apply_command_id = command.context.command_id
        extension.apply_correlation_id = command.context.correlation_id
        db.flush()
        _stage_lifecycle_evidence(
            db,
            extension=extension,
            context=command.context,
            action="billing.service_extension_applied",
            event_type=EventType.service_extension_applied,
            occurred_at=now,
            previous_status=previous_status,
            idempotency_key_sha256=_sha256(idempotency_key),
        )
        return _apply_outcome(extension, replayed=False)

    return execute_owner_command(
        db,
        definition=_APPLY_COMMAND,
        context=command.context,
        operation=operation,
    )


def repair_service_extension_anchor_projection(
    db: Session,
    command: RepairServiceExtensionAnchorProjectionCommand,
) -> RepairServiceExtensionAnchorProjectionOutcome:
    """Advance non-terminal anchors that drifted below immutable entry evidence."""

    def operation() -> RepairServiceExtensionAnchorProjectionOutcome:
        from app.services.audit_adapter import stage_audit_event
        from app.services.audit_helpers import resolve_actor_label_from_db
        from app.services.events import emit_event

        idempotency_key = _require_command_context(
            command.context,
            expected_scope=APPLY_SCOPE,
        )
        extension = get_extension(db, command.extension_id, lock=True)
        if extension.status != ServiceExtensionStatus.applied:
            _error(
                "transition_conflict",
                "Only an applied service extension has a repairable anchor projection.",
                current_status=extension.status.value,
            )
        entries = list(
            db.scalars(
                select(ServiceExtensionEntry)
                .where(ServiceExtensionEntry.extension_id == extension.id)
                .order_by(
                    ServiceExtensionEntry.subscription_id,
                    ServiceExtensionEntry.id,
                )
                .with_for_update()
            ).all()
        )
        subscription_ids = sorted(
            {entry.subscription_id for entry in entries},
            key=str,
        )
        subscriptions = {
            subscription.id: subscription
            for subscription in db.scalars(
                select(Subscription)
                .where(Subscription.id.in_(subscription_ids))
                .order_by(Subscription.id)
                .with_for_update()
            ).all()
        }
        repaired = 0
        for entry in entries:
            subscription = subscriptions.get(entry.subscription_id)
            target = entry.new_next_billing_at
            if (
                subscription is None
                or subscription.status
                not in (SubscriptionStatus.active, SubscriptionStatus.suspended)
                or target is None
            ):
                continue
            current = subscription.next_billing_at
            if current is not None and _as_utc(current) >= _as_utc(target):
                continue
            subscription.next_billing_at = target
            repaired += 1
        if repaired:
            now = datetime.now(UTC)
            db.flush()
            actor_type, actor_id = _actor(command.context)
            actor_label = resolve_actor_label_from_db(db, actor_id, actor_type)
            if not actor_label:
                actor_label = {
                    AuditActorType.api_key: "Integration",
                    AuditActorType.service: "Automated service",
                    AuditActorType.system: "System",
                    AuditActorType.user: "Staff member",
                }[actor_type]
            metadata: dict[str, object] = {
                "schema_version": 1,
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
                "idempotency_key_sha256": _sha256(idempotency_key),
                "resulting_status": extension.status.value,
                "inspected": len(entries),
                "repaired": repaired,
            }
            stage_audit_event(
                db,
                action="billing.service_extension_anchor_repaired",
                entity_type="service_extension",
                entity_id=str(extension.id),
                actor_type=actor_type,
                actor_id=actor_id,
                actor_label=actor_label,
                request_id=str(command.context.correlation_id),
                occurred_at=now,
                metadata=metadata,
            )
            emit_event(
                db,
                EventType.service_extension_anchor_repaired,
                {
                    "schema_version": 1,
                    "extension_id": str(extension.id),
                    **metadata,
                },
                actor=command.context.actor,
            )
        return RepairServiceExtensionAnchorProjectionOutcome(
            extension_id=extension.id,
            status=extension.status,
            inspected_count=len(entries),
            repaired_count=repaired,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
        )

    return execute_owner_command(
        db,
        definition=_REPAIR_ANCHOR_COMMAND,
        context=command.context,
        operation=operation,
    )


def _shield_window_end(created_at: datetime, days: int) -> datetime:
    start = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    return start + timedelta(days=days)


def extension_shield_reason(db: Session, account_id: str | uuid.UUID) -> str | None:
    """Why billing enforcement should skip this account, or None.

    An applied service extension grants N days of service regardless of
    arrears (outage compensation / goodwill). Until those N days elapse from
    the moment the extension was applied, dunning must not suspend the
    account — otherwise enforcement undoes the extension within hours, which
    is exactly what happened at cutover.
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
    now = datetime.now(UTC)
    rows = db.execute(
        select(
            ServiceExtensionEntry.subscriber_id,
            ServiceExtensionEntry.created_at,
            ServiceExtension.days,
            ServiceExtension.id,
        )
        .join(
            ServiceExtension, ServiceExtension.id == ServiceExtensionEntry.extension_id
        )
        .where(
            ServiceExtensionEntry.subscriber_id.in_(ids),
            ServiceExtension.status == ServiceExtensionStatus.applied,
            ServiceExtensionEntry.created_at
            >= now - timedelta(days=MAX_ALLOWED_EXTENSION_DAYS),
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for subscriber_id, created_at, days, extension_id in rows:
        until = _shield_window_end(created_at, int(days))
        if until > now:
            reasons.setdefault(
                subscriber_id,
                f"service extension {extension_id} in force until {until.date().isoformat()}",
            )
    return reasons


def scope_options(db: Session) -> ServiceExtensionScopeOptions:
    """POP sites and NAS devices for the extension form's scope selectors."""
    from app.models.catalog import NasDevice
    from app.models.network_monitoring import PopSite

    return ServiceExtensionScopeOptions(
        pop_sites=tuple(
            ServiceExtensionScopeChoice(id=item.id, label=item.name)
            for item in db.scalars(select(PopSite).order_by(PopSite.name)).all()
        ),
        nas_devices=tuple(
            ServiceExtensionScopeChoice(id=item.id, label=item.name)
            for item in db.scalars(select(NasDevice).order_by(NasDevice.name)).all()
        ),
        scope_types=tuple(ServiceExtensionScope),
        max_days=_max_extension_days(db),
    )


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
