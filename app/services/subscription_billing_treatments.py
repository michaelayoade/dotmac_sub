"""Owner of subscription-scoped complimentary and sponsored billing treatment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import BillingCycle, BillingMode, Subscription
from app.models.domain_settings import SettingDomain
from app.models.subscription_billing_treatment import (
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
    SubscriptionBillingTreatment,
)
from app.services import settings_spec
from app.services.audit_adapter import stage_audit_event
from app.services.billing_settings import COLLECTIBLE_SERVICE_STATUSES
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

TREATMENT_WRITE_SCOPE = "billing:treatment:write"
TREATMENT_MAX_DAYS_SETTING = "subscription_billing_treatment_max_days"

_CREATE_COMMAND = OwnerCommandDefinition(
    owner="financial.subscription_billing_treatments",
    concern="subscription billing-treatment lifecycle",
    name="create_subscription_billing_treatment",
)
_REVOKE_COMMAND = OwnerCommandDefinition(
    owner="financial.subscription_billing_treatments",
    concern="subscription billing-treatment lifecycle",
    name="revoke_subscription_billing_treatment",
)


class BillingTreatmentDecisionStatus(StrEnum):
    standard = "standard"
    effective = "effective"
    protected_drift = "protected_drift"


class SubscriptionBillingTreatmentError(DomainError):
    """Stable transport-neutral billing-treatment failure."""


def _error(
    suffix: str, message: str, **details: object
) -> SubscriptionBillingTreatmentError:
    return SubscriptionBillingTreatmentError(
        code=f"financial.subscription_billing_treatments.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class SubscriptionReferencePrice:
    amount: Decimal
    currency: str
    billing_cycle: BillingCycle


@dataclass(frozen=True, slots=True)
class BillingTreatmentDecision:
    subscription_id: UUID
    account_id: UUID
    status: BillingTreatmentDecisionStatus
    treatment: SubscriptionBillingTreatment
    arrangement_id: UUID | None = None
    authorized_offer_id: UUID | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    maximum_recurring_amount: Decimal | None = None
    billing_cycle: BillingCycle | None = None
    currency: str | None = None
    reason_code: BillingTreatmentReason | None = None
    reason: str | None = None
    drift_reason: str | None = None

    @property
    def suppress_customer_billing(self) -> bool:
        return self.status != BillingTreatmentDecisionStatus.standard

    @property
    def grantable(self) -> bool:
        return self.status == BillingTreatmentDecisionStatus.effective


@dataclass(frozen=True, slots=True)
class BillingTreatmentPreview:
    subscription_id: UUID
    account_id: UUID
    authorized_offer_id: UUID
    treatment: SubscriptionBillingTreatment
    reason_code: BillingTreatmentReason
    reason: str
    starts_at: datetime
    ends_at: datetime
    approval_policy_max_days: int
    maximum_recurring_amount: Decimal
    currency: str
    billing_cycle: BillingCycle
    sponsor_reference: str | None
    cost_center: str | None
    evaluated_at: datetime
    fingerprint: str


@dataclass(frozen=True, slots=True)
class CreateBillingTreatmentCommand:
    context: CommandContext
    subscription_id: UUID
    treatment: SubscriptionBillingTreatment
    reason_code: BillingTreatmentReason
    reason: str
    starts_at: datetime
    ends_at: datetime
    sponsor_reference: str | None
    cost_center: str | None
    preview_effective_at: datetime
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class BillingTreatmentOutcome:
    arrangement_id: UUID
    subscription_id: UUID
    account_id: UUID
    treatment: SubscriptionBillingTreatment
    starts_at: datetime
    ends_at: datetime
    approval_policy_max_days: int
    maximum_recurring_amount: Decimal
    billing_cycle: BillingCycle
    currency: str
    status: BillingTreatmentStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class RevokeBillingTreatmentCommand:
    context: CommandContext
    arrangement_id: UUID
    reason: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clean(value: str | None, *, limit: int) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned[:limit] if cleaned else None


def _require_text(value: str, *, field: str, limit: int) -> str:
    cleaned = _clean(value, limit=limit)
    if cleaned is None:
        raise _error("invalid_command", f"{field} is required.", field=field)
    return cleaned


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def resolve_billing_treatment_max_days(db: Session) -> int:
    """Resolve the registered annual-or-shorter reapproval horizon."""
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, TREATMENT_MAX_DAYS_SETTING
    )
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _error(
            "invalid_approval_policy",
            "The billing-treatment approval horizon is unavailable.",
            setting=TREATMENT_MAX_DAYS_SETTING,
        )
    return value


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


def resolve_subscription_reference_price(
    db: Session,
    subscription: Subscription,
    *,
    effective_at: datetime,
) -> SubscriptionReferencePrice:
    """Resolve the positive contracted value retained for waiver reporting."""
    from app.services.billing_automation import _effective_unit_price, _resolve_price

    if subscription.billing_mode == BillingMode.prepaid and (
        subscription.unit_price is None or subscription.unit_price <= 0
    ):
        raise _error(
            "missing_contract_price",
            "The prepaid subscription has no positive contracted price evidence.",
            subscription_id=str(subscription.id),
        )
    catalog_amount, currency, cycle = _resolve_price(db, subscription)
    if catalog_amount is None:
        raise _error(
            "missing_contract_price",
            "The subscription has no active recurring price.",
            subscription_id=str(subscription.id),
        )
    amount = round_money(
        _effective_unit_price(subscription, catalog_amount, _utc(effective_at))
    )
    normalized_currency = str(currency or "").strip().upper()
    resolved_cycle = cycle or subscription.billing_cycle
    if amount <= Decimal("0.00") or resolved_cycle is None:
        raise _error(
            "missing_contract_price",
            "The subscription has no positive recurring service value.",
            subscription_id=str(subscription.id),
        )
    if len(normalized_currency) != 3:
        raise _error(
            "invalid_currency",
            "The subscription recurring price has an invalid currency.",
            subscription_id=str(subscription.id),
        )
    return SubscriptionReferencePrice(amount, normalized_currency, resolved_cycle)


def _aligned_end(starts_at: datetime, ends_at: datetime, cycle: BillingCycle) -> bool:
    from app.services.billing_automation import _period_end

    if ends_at <= starts_at:
        return False
    if cycle == BillingCycle.daily:
        return (ends_at - starts_at) % timedelta(days=1) == timedelta(0)
    if cycle == BillingCycle.weekly:
        return (ends_at - starts_at) % timedelta(weeks=1) == timedelta(0)
    months_per_cycle = {
        BillingCycle.monthly: 1,
        BillingCycle.quarterly: 3,
        BillingCycle.annual: 12,
    }[cycle]
    months_apart = (ends_at.year - starts_at.year) * 12 + (
        ends_at.month - starts_at.month
    )
    candidate_cycles = max(months_apart // months_per_cycle + 2, 1)
    cursor = starts_at
    for _ in range(candidate_cycles):
        cursor = _period_end(cursor, cycle)
        if cursor == ends_at:
            return True
        if cursor > ends_at:
            return False
    return False


def _overlap_query(subscription_id: UUID, starts_at: datetime, ends_at: datetime):
    return select(SubscriptionBillingArrangement.id).where(
        SubscriptionBillingArrangement.subscription_id == subscription_id,
        SubscriptionBillingArrangement.status == BillingTreatmentStatus.active,
        SubscriptionBillingArrangement.ends_at > starts_at,
        SubscriptionBillingArrangement.starts_at < ends_at,
    )


def preview_subscription_billing_treatment(
    db: Session,
    *,
    subscription_id: UUID,
    treatment: SubscriptionBillingTreatment,
    reason_code: BillingTreatmentReason,
    reason: str,
    starts_at: datetime,
    ends_at: datetime | None,
    sponsor_reference: str | None,
    cost_center: str | None,
    evaluated_at: datetime | None = None,
) -> BillingTreatmentPreview:
    observed_at = _utc(evaluated_at or datetime.now(UTC))
    start = _utc(starts_at)
    if ends_at is None:
        raise _error(
            "finite_period_required",
            "Billing treatment requires a finite end and periodic reapproval.",
        )
    end = _utc(ends_at)
    if treatment == SubscriptionBillingTreatment.standard:
        raise _error(
            "invalid_treatment",
            "Standard billing is restored by revoking the active arrangement.",
        )
    if start < observed_at - timedelta(minutes=5):
        raise _error(
            "retroactive_treatment", "Billing treatment must start prospectively."
        )
    if end <= start:
        raise _error("invalid_period", "Treatment end must be after its start.")
    maximum_days = resolve_billing_treatment_max_days(db)
    if end - start > timedelta(days=maximum_days):
        raise _error(
            "approval_horizon_exceeded",
            "Billing treatment exceeds the maximum approval horizon.",
            maximum_days=maximum_days,
            setting=TREATMENT_MAX_DAYS_SETTING,
        )
    normalized_reason = _require_text(reason, field="reason", limit=2000)
    sponsor = _clean(sponsor_reference, limit=200)
    center = _clean(cost_center, limit=100)
    if treatment == SubscriptionBillingTreatment.sponsored and not (sponsor or center):
        raise _error(
            "missing_sponsor_evidence",
            "Sponsored treatment requires a sponsor reference or cost centre.",
        )
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise _error(
            "subscription_not_found",
            "The subscription was not found.",
            subscription_id=str(subscription_id),
        )
    if subscription.status not in COLLECTIBLE_SERVICE_STATUSES:
        raise _error(
            "subscription_not_collectible",
            "Only a collectible service can receive a billing treatment.",
            subscription_id=str(subscription.id),
            status=subscription.status.value,
        )
    if db.scalar(_overlap_query(subscription.id, start, end)) is not None:
        raise _error(
            "overlapping_treatment",
            "The subscription already has an overlapping billing treatment.",
            subscription_id=str(subscription.id),
        )
    reference = resolve_subscription_reference_price(
        db, subscription, effective_at=start
    )
    billing_anchor = subscription.next_billing_at or subscription.start_at
    if billing_anchor is None:
        raise _error(
            "missing_billing_anchor",
            "The subscription has no billing boundary for treatment alignment.",
            subscription_id=str(subscription.id),
        )
    anchor = _utc(billing_anchor)
    if start < anchor or (
        start != anchor and not _aligned_end(anchor, start, reference.billing_cycle)
    ):
        raise _error(
            "unaligned_start",
            "Treatment must start on a subscription billing boundary.",
            billing_cycle=reference.billing_cycle.value,
            billing_anchor=anchor.isoformat(),
        )
    if not _aligned_end(start, end, reference.billing_cycle):
        raise _error(
            "unaligned_period",
            "Treatment end must align with a complete subscription billing cycle.",
            billing_cycle=reference.billing_cycle.value,
        )
    payload = {
        "subscription_id": str(subscription.id),
        "account_id": str(subscription.subscriber_id),
        "authorized_offer_id": str(subscription.offer_id),
        "treatment": treatment.value,
        "reason_code": reason_code.value,
        "reason": normalized_reason,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "approval_policy_max_days": maximum_days,
        "maximum_recurring_amount": str(reference.amount),
        "currency": reference.currency,
        "billing_cycle": reference.billing_cycle.value,
        "sponsor_reference": sponsor,
        "cost_center": center,
    }
    return BillingTreatmentPreview(
        subscription_id=subscription.id,
        account_id=subscription.subscriber_id,
        authorized_offer_id=subscription.offer_id,
        treatment=treatment,
        reason_code=reason_code,
        reason=normalized_reason,
        starts_at=start,
        ends_at=end,
        approval_policy_max_days=maximum_days,
        maximum_recurring_amount=reference.amount,
        currency=reference.currency,
        billing_cycle=reference.billing_cycle,
        sponsor_reference=sponsor,
        cost_center=center,
        evaluated_at=observed_at,
        fingerprint=_sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))),
    )


def _outcome(
    arrangement: SubscriptionBillingArrangement, *, replayed: bool
) -> BillingTreatmentOutcome:
    return BillingTreatmentOutcome(
        arrangement_id=arrangement.id,
        subscription_id=arrangement.subscription_id,
        account_id=arrangement.account_id,
        treatment=arrangement.treatment,
        starts_at=arrangement.starts_at,
        ends_at=arrangement.ends_at,
        approval_policy_max_days=arrangement.approval_policy_max_days,
        maximum_recurring_amount=arrangement.maximum_recurring_amount,
        billing_cycle=arrangement.billing_cycle,
        currency=arrangement.currency,
        status=arrangement.status,
        replayed=replayed,
    )


def create_subscription_billing_treatment(
    db: Session, command: CreateBillingTreatmentCommand
) -> BillingTreatmentOutcome:
    """Approve one non-overlapping arrangement through the canonical writer."""

    def operation() -> BillingTreatmentOutcome:
        if command.context.scope != TREATMENT_WRITE_SCOPE:
            raise _error("invalid_scope", "Billing-treatment write scope is required.")
        raw_key = _require_text(
            command.context.idempotency_key or "", field="idempotency_key", limit=500
        )
        key_hash = _sha256(raw_key)
        existing = db.scalar(
            select(SubscriptionBillingArrangement).where(
                SubscriptionBillingArrangement.idempotency_key_sha256 == key_hash
            )
        )
        if existing is not None:
            if existing.command_fingerprint != command.preview_fingerprint:
                raise _error(
                    "idempotency_conflict",
                    "The idempotency key belongs to another billing treatment.",
                )
            return _outcome(existing, replayed=True)
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        if subscription is None:
            raise _error("subscription_not_found", "The subscription was not found.")
        preview = preview_subscription_billing_treatment(
            db,
            subscription_id=subscription.id,
            treatment=command.treatment,
            reason_code=command.reason_code,
            reason=command.reason,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            sponsor_reference=command.sponsor_reference,
            cost_center=command.cost_center,
            evaluated_at=command.preview_effective_at,
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise _error(
                "stale_preview",
                "The subscription or treatment evidence changed; preview again.",
                current_fingerprint=preview.fingerprint,
            )
        arrangement = SubscriptionBillingArrangement(
            subscription_id=preview.subscription_id,
            account_id=preview.account_id,
            authorized_offer_id=preview.authorized_offer_id,
            treatment=preview.treatment,
            reason_code=preview.reason_code,
            reason=preview.reason,
            starts_at=preview.starts_at,
            ends_at=preview.ends_at,
            approval_policy_max_days=preview.approval_policy_max_days,
            maximum_recurring_amount=preview.maximum_recurring_amount,
            billing_cycle=preview.billing_cycle,
            currency=preview.currency,
            sponsor_reference=preview.sponsor_reference,
            cost_center=preview.cost_center,
            status=BillingTreatmentStatus.active,
            approved_by=command.context.actor,
            approved_at=datetime.now(UTC),
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            idempotency_key_sha256=key_hash,
            command_fingerprint=preview.fingerprint,
        )
        db.add(arrangement)
        db.flush()
        actor_type, actor_id = _actor(command.context)
        metadata = {
            "schema_version": 1,
            "arrangement_id": str(arrangement.id),
            "subscription_id": str(arrangement.subscription_id),
            "account_id": str(arrangement.account_id),
            "authorized_offer_id": str(arrangement.authorized_offer_id),
            "treatment": arrangement.treatment.value,
            "reason_code": arrangement.reason_code.value,
            "starts_at": arrangement.starts_at.isoformat(),
            "ends_at": arrangement.ends_at.isoformat(),
            "approval_policy_max_days": arrangement.approval_policy_max_days,
            "maximum_recurring_amount": str(arrangement.maximum_recurring_amount),
            "billing_cycle": arrangement.billing_cycle.value,
            "currency": arrangement.currency,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        }
        stage_audit_event(
            db,
            action="financial.subscription_billing_treatment_created",
            entity_type="subscription_billing_arrangement",
            entity_id=str(arrangement.id),
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=str(command.context.correlation_id),
            metadata=metadata,
        )
        emit_event(
            db,
            EventType.subscription_billing_treatment_changed,
            {**metadata, "action": "created"},
            actor=command.context.actor,
            account_id=arrangement.account_id,
            subscription_id=arrangement.subscription_id,
        )
        return _outcome(arrangement, replayed=False)

    return execute_owner_command(
        db, definition=_CREATE_COMMAND, context=command.context, operation=operation
    )


def revoke_subscription_billing_treatment(
    db: Session, command: RevokeBillingTreatmentCommand
) -> BillingTreatmentOutcome:
    """Prospectively restore standard billing without erasing granted periods."""

    def operation() -> BillingTreatmentOutcome:
        if command.context.scope != TREATMENT_WRITE_SCOPE:
            raise _error("invalid_scope", "Billing-treatment write scope is required.")
        reason = _require_text(command.reason, field="reason", limit=2000)
        raw_key = _require_text(
            command.context.idempotency_key or "", field="idempotency_key", limit=500
        )
        key_hash = _sha256(raw_key)
        replay = db.scalar(
            select(SubscriptionBillingArrangement).where(
                SubscriptionBillingArrangement.revocation_idempotency_key_sha256
                == key_hash
            )
        )
        if replay is not None:
            if replay.id != command.arrangement_id:
                raise _error(
                    "idempotency_conflict",
                    "The idempotency key belongs to another revocation.",
                )
            return _outcome(replay, replayed=True)
        arrangement = db.scalar(
            select(SubscriptionBillingArrangement)
            .where(SubscriptionBillingArrangement.id == command.arrangement_id)
            .with_for_update()
        )
        if arrangement is None:
            raise _error(
                "arrangement_not_found", "The billing treatment was not found."
            )
        if arrangement.status == BillingTreatmentStatus.revoked:
            raise _error(
                "invalid_transition", "The billing treatment is already revoked."
            )
        now = datetime.now(UTC)
        arrangement.status = BillingTreatmentStatus.revoked
        arrangement.revoked_by = command.context.actor
        arrangement.revoked_at = now
        arrangement.revocation_reason = reason
        arrangement.revocation_command_id = command.context.command_id
        arrangement.revocation_correlation_id = command.context.correlation_id
        arrangement.revocation_idempotency_key_sha256 = key_hash
        db.flush()
        actor_type, actor_id = _actor(command.context)
        metadata = {
            "schema_version": 1,
            "arrangement_id": str(arrangement.id),
            "subscription_id": str(arrangement.subscription_id),
            "account_id": str(arrangement.account_id),
            "treatment": arrangement.treatment.value,
            "revoked_at": now.isoformat(),
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        }
        stage_audit_event(
            db,
            action="financial.subscription_billing_treatment_revoked",
            entity_type="subscription_billing_arrangement",
            entity_id=str(arrangement.id),
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=str(command.context.correlation_id),
            metadata=metadata,
        )
        emit_event(
            db,
            EventType.subscription_billing_treatment_changed,
            {**metadata, "action": "revoked"},
            actor=command.context.actor,
            account_id=arrangement.account_id,
            subscription_id=arrangement.subscription_id,
        )
        return _outcome(arrangement, replayed=False)

    return execute_owner_command(
        db, definition=_REVOKE_COMMAND, context=command.context, operation=operation
    )


def resolve_subscription_billing_treatments(
    db: Session,
    subscriptions: list[Subscription],
    *,
    as_of: datetime | None = None,
) -> dict[UUID, BillingTreatmentDecision]:
    """Resolve one customer-billing answer for a bounded service cohort."""
    observed_at = _utc(as_of or datetime.now(UTC))
    if not subscriptions:
        return {}
    rows = list(
        db.scalars(
            select(SubscriptionBillingArrangement)
            .where(
                SubscriptionBillingArrangement.subscription_id.in_(
                    [item.id for item in subscriptions]
                ),
                SubscriptionBillingArrangement.status == BillingTreatmentStatus.active,
                SubscriptionBillingArrangement.starts_at <= observed_at,
                SubscriptionBillingArrangement.ends_at > observed_at,
            )
            .order_by(
                SubscriptionBillingArrangement.subscription_id,
                SubscriptionBillingArrangement.starts_at.desc(),
                SubscriptionBillingArrangement.id.desc(),
            )
        ).all()
    )
    grouped: dict[UUID, list[SubscriptionBillingArrangement]] = {}
    for row in rows:
        grouped.setdefault(row.subscription_id, []).append(row)
    decisions: dict[UUID, BillingTreatmentDecision] = {}
    for subscription in subscriptions:
        arrangements = grouped.get(subscription.id, [])
        if not arrangements:
            decisions[subscription.id] = BillingTreatmentDecision(
                subscription.id,
                subscription.subscriber_id,
                BillingTreatmentDecisionStatus.standard,
                SubscriptionBillingTreatment.standard,
            )
            continue
        arrangement = arrangements[0]
        drift_reason = None
        if len(arrangements) > 1:
            drift_reason = "overlapping_effective_arrangements"
        elif arrangement.account_id != subscription.subscriber_id:
            drift_reason = "account_mismatch"
        elif arrangement.authorized_offer_id != subscription.offer_id:
            drift_reason = "unauthorized_offer_change"
        else:
            try:
                reference = resolve_subscription_reference_price(
                    db, subscription, effective_at=observed_at
                )
            except SubscriptionBillingTreatmentError as exc:
                drift_reason = exc.code.rsplit(".", maxsplit=1)[-1]
            else:
                if reference.amount > arrangement.maximum_recurring_amount:
                    drift_reason = "approved_value_exceeded"
                elif reference.currency != arrangement.currency:
                    drift_reason = "currency_mismatch"
                elif reference.billing_cycle != arrangement.billing_cycle:
                    drift_reason = "billing_cycle_mismatch"
        decisions[subscription.id] = BillingTreatmentDecision(
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            status=(
                BillingTreatmentDecisionStatus.protected_drift
                if drift_reason
                else BillingTreatmentDecisionStatus.effective
            ),
            treatment=arrangement.treatment,
            arrangement_id=arrangement.id,
            authorized_offer_id=arrangement.authorized_offer_id,
            starts_at=arrangement.starts_at,
            ends_at=arrangement.ends_at,
            maximum_recurring_amount=arrangement.maximum_recurring_amount,
            billing_cycle=arrangement.billing_cycle,
            currency=arrangement.currency,
            reason_code=arrangement.reason_code,
            reason=arrangement.reason,
            drift_reason=drift_reason,
        )
    return decisions


def resolve_subscription_billing_treatment(
    db: Session, subscription: Subscription, *, as_of: datetime | None = None
) -> BillingTreatmentDecision:
    return resolve_subscription_billing_treatments(db, [subscription], as_of=as_of)[
        subscription.id
    ]


def list_subscription_billing_arrangements(
    db: Session, *, subscription_id: UUID
) -> tuple[SubscriptionBillingArrangement, ...]:
    return tuple(
        db.scalars(
            select(SubscriptionBillingArrangement)
            .where(SubscriptionBillingArrangement.subscription_id == subscription_id)
            .order_by(
                SubscriptionBillingArrangement.starts_at.desc(),
                SubscriptionBillingArrangement.id.desc(),
            )
        ).all()
    )


def subscription_has_open_billing_treatment(
    db: Session, subscription_id: UUID, *, as_of: datetime | None = None
) -> bool:
    observed_at = _utc(as_of or datetime.now(UTC))
    return (
        db.scalar(
            select(SubscriptionBillingArrangement.id)
            .where(
                SubscriptionBillingArrangement.subscription_id == subscription_id,
                SubscriptionBillingArrangement.status == BillingTreatmentStatus.active,
                SubscriptionBillingArrangement.ends_at > observed_at,
            )
            .limit(1)
        )
        is not None
    )
