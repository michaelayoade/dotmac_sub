"""Canonical read model and command contract for subscription lifecycle changes.

This boundary does not own billing ledger writes, RADIUS projection, or catalog
mutation. It composes those owners into one preview that every UI, API, bulk
job, and scheduler can consume before calling the existing command executors.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.catalog import (
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.access_resolution import resolve_customer_access
from app.services.common import coerce_uuid, round_money
from app.services.subscription_lifecycle_policy import (
    BILLING_COLLECTIBLE_SERVICE_STATUSES,
    MRR_COUNTABLE_SERVICE_STATUSES,
    TERMINAL_SERVICE_STATUSES,
)

_SUSPENDED_EQUIVALENT_STATUSES = frozenset(
    {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
    }
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class SubscriptionLifecycleError(ValueError):
    pass


class SubscriptionLifecycleHeadConflict(SubscriptionLifecycleError):
    pass


class SubscriptionCommandKind(str, enum.Enum):
    activate = "activate"
    suspend = "suspend"
    restore = "restore"
    renew = "renew"
    cancel = "cancel"
    expire = "expire"
    change_plan = "change_plan"


class SubscriptionEffectiveTiming(str, enum.Enum):
    immediate = "immediate"
    next_cycle = "next_cycle"
    scheduled = "scheduled"


class SubscriptionCommandOutcomeStatus(str, enum.Enum):
    applied = "applied"
    scheduled = "scheduled"
    skipped = "skipped"
    rejected = "rejected"
    failed = "failed"
    superseded = "superseded"


class SubscriptionSessionAction(str, enum.Enum):
    none = "none"
    authorize = "authorize"
    disconnect = "disconnect"
    reauthorize = "reauthorize"
    deprovision = "deprovision"


@dataclass(frozen=True)
class SubscriptionLifecycleCommand:
    subscription_id: str
    kind: SubscriptionCommandKind
    source: str
    effective_timing: SubscriptionEffectiveTiming = (
        SubscriptionEffectiveTiming.immediate
    )
    effective_at: datetime | None = None
    target_offer_id: str | None = None
    reason: str | None = None
    expected_head: str | None = None
    expected_financial_fingerprint: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SubscriptionCommandKind):
            object.__setattr__(self, "kind", SubscriptionCommandKind(self.kind))
        if not isinstance(self.effective_timing, SubscriptionEffectiveTiming):
            object.__setattr__(
                self,
                "effective_timing",
                SubscriptionEffectiveTiming(self.effective_timing),
            )
        for name in ("subscription_id", "source"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise SubscriptionLifecycleError(f"{name} is required")
            object.__setattr__(self, name, value)
        if self.kind == SubscriptionCommandKind.change_plan:
            target_offer_id = str(self.target_offer_id or "").strip()
            if not target_offer_id:
                raise SubscriptionLifecycleError(
                    "target_offer_id is required for change_plan"
                )
            object.__setattr__(self, "target_offer_id", target_offer_id)
        elif self.target_offer_id is not None:
            raise SubscriptionLifecycleError(
                "target_offer_id is only valid for change_plan"
            )
        if (
            self.effective_timing == SubscriptionEffectiveTiming.scheduled
            and self.effective_at is None
        ):
            raise SubscriptionLifecycleError(
                "effective_at is required for scheduled commands"
            )
        if (
            self.effective_timing == SubscriptionEffectiveTiming.next_cycle
            and self.effective_at is not None
        ):
            raise SubscriptionLifecycleError(
                "effective_at is not valid for next_cycle commands; use scheduled"
            )
        if self.reason is not None:
            object.__setattr__(self, "reason", self.reason.strip() or None)
        if self.expected_head is not None:
            object.__setattr__(
                self, "expected_head", self.expected_head.strip() or None
            )
        if self.expected_financial_fingerprint is not None:
            object.__setattr__(
                self,
                "expected_financial_fingerprint",
                self.expected_financial_fingerprint.strip() or None,
            )
        if self.idempotency_key is not None:
            object.__setattr__(
                self, "idempotency_key", self.idempotency_key.strip() or None
            )


@dataclass(frozen=True)
class SubscriptionLifecycleState:
    status: str
    offer_id: str
    offer_name: str | None
    billing_mode: str
    billing_collectible: bool
    mrr_countable: bool
    radius_access_state: str | None
    radius_allowed: bool
    radius_blocked: bool
    access_block_reason: str | None
    terminal: bool


@dataclass(frozen=True)
class PendingSubscriptionChange:
    request_id: str
    target_offer_id: str
    target_offer_name: str | None
    effective_at: datetime


@dataclass(frozen=True)
class SubscriptionLifecycleSnapshot:
    subscription_id: str
    account_id: str
    account_status: str | None
    account_enabled: bool
    head: str
    state: SubscriptionLifecycleState
    start_at: datetime | None
    end_at: datetime | None
    next_billing_at: datetime | None
    canceled_at: datetime | None
    pending_change: PendingSubscriptionChange | None


@dataclass(frozen=True)
class SubscriptionBillingImpact:
    action: str
    collectible_before: bool
    collectible_after: bool
    currency: str | None = None
    charge_amount: Decimal = Decimal("0.00")
    credit_amount: Decimal = Decimal("0.00")
    net_amount: Decimal = Decimal("0.00")
    available_balance: Decimal | None = None
    required_amount: Decimal | None = None
    shortfall: Decimal | None = None
    collection_blocking_balance: Decimal | None = None
    possible_prorated_credit: bool = False
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class SubscriptionAccessImpact:
    current_state: str | None
    proposed_state: str | None
    allowed_before: bool
    allowed_after: bool
    session_action: SubscriptionSessionAction
    block_reason_after: str | None


@dataclass(frozen=True)
class SubscriptionLifecyclePreview:
    command: SubscriptionLifecycleCommand
    current: SubscriptionLifecycleSnapshot
    proposed: SubscriptionLifecycleState
    effective_at: datetime
    eligible: bool
    eligibility_reasons: tuple[str, ...]
    billing_impact: SubscriptionBillingImpact
    access_impact: SubscriptionAccessImpact
    requires_confirmation: bool = True


@dataclass(frozen=True)
class SubscriptionCommandOutcome:
    command: SubscriptionLifecycleCommand
    status: SubscriptionCommandOutcomeStatus
    message: str
    previous_head: str
    current_head: str | None = None
    artifact_ids: tuple[str, ...] = ()
    error_code: str | None = None
    replayed: bool = False


def assert_subscription_transition(
    from_status: SubscriptionStatus | None,
    to_status: SubscriptionStatus | None,
) -> None:
    """Preserve terminal subscription states as immutable sinks."""
    if to_status is None or from_status == to_status:
        return
    if from_status in TERMINAL_SERVICE_STATUSES:
        raise SubscriptionLifecycleError(
            f"Illegal subscription transition {from_status.value} -> "
            f"{to_status.value}: {from_status.value} is terminal and cannot be "
            "reactivated"
        )


def assert_subscription_head(
    snapshot: SubscriptionLifecycleSnapshot,
    expected_head: str | None,
) -> None:
    if expected_head is not None and expected_head != snapshot.head:
        raise SubscriptionLifecycleHeadConflict(
            "Subscription changed after this action was reviewed; refresh and "
            "review the current state before applying it"
        )


def resolve_subscription_lifecycle(
    db: Session,
    subscription_id: str,
) -> SubscriptionLifecycleSnapshot:
    subscription = db.scalar(
        select(Subscription)
        .where(Subscription.id == coerce_uuid(subscription_id))
        .options(
            selectinload(Subscription.subscriber),
            selectinload(Subscription.offer),
        )
    )
    if subscription is None:
        raise SubscriptionLifecycleError("Subscription not found")
    pending_change = db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id == subscription.id)
        .where(
            SubscriptionChangeRequest.status.in_(
                {
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                }
            )
        )
        .where(SubscriptionChangeRequest.applied_at.is_(None))
        .where(SubscriptionChangeRequest.is_active.is_(True))
        .order_by(SubscriptionChangeRequest.effective_date.asc())
        .options(selectinload(SubscriptionChangeRequest.requested_offer))
    )
    subscriber = subscription.subscriber
    return SubscriptionLifecycleSnapshot(
        subscription_id=str(subscription.id),
        account_id=str(subscription.subscriber_id),
        account_status=_enum_value(getattr(subscriber, "status", None)),
        account_enabled=bool(subscriber and subscriber.is_active),
        head=_subscription_head(
            db,
            subscription,
            subscriber=subscriber,
            pending_change=pending_change,
        ),
        state=_resolve_state(subscription, subscriber=subscriber),
        start_at=_aware_utc(subscription.start_at),
        end_at=_aware_utc(subscription.end_at),
        next_billing_at=_aware_utc(subscription.next_billing_at),
        canceled_at=_aware_utc(subscription.canceled_at),
        pending_change=(
            PendingSubscriptionChange(
                request_id=str(pending_change.id),
                target_offer_id=str(pending_change.requested_offer_id),
                target_offer_name=(
                    pending_change.requested_offer.name
                    if pending_change.requested_offer
                    else None
                ),
                effective_at=datetime.combine(
                    pending_change.effective_date,
                    datetime.min.time(),
                    tzinfo=UTC,
                ),
            )
            if pending_change is not None
            else None
        ),
    )


def preview_subscription_command(
    db: Session,
    command: SubscriptionLifecycleCommand,
    *,
    now: datetime | None = None,
    current_balance: Decimal | None = None,
) -> SubscriptionLifecyclePreview:
    effective_now = _aware_utc(now) or datetime.now(UTC)
    current = resolve_subscription_lifecycle(db, command.subscription_id)
    assert_subscription_head(current, command.expected_head)
    subscription = db.get(Subscription, coerce_uuid(command.subscription_id))
    if subscription is None:  # pragma: no cover - resolver already proves existence
        raise SubscriptionLifecycleError("Subscription not found")
    target_offer = _target_offer(db, command)
    reasons = _eligibility_reasons(
        db,
        subscription,
        command,
        target_offer=target_offer,
        now=effective_now,
    )
    proposed_status = _proposed_status(subscription.status, command.kind)
    proposed_offer = target_offer or subscription.offer
    proposed = _resolve_state(
        subscription,
        subscriber=subscription.subscriber,
        status=proposed_status,
        offer=proposed_offer,
    )
    effective_at = _effective_at(current, command, now=effective_now)
    billing_impact, financial_reason = _billing_impact(
        db,
        subscription,
        command,
        current=current,
        proposed=proposed,
        target_offer=target_offer,
        effective_at=effective_at,
        current_balance=current_balance,
    )
    if financial_reason:
        reasons.append(financial_reason)
    access_impact = SubscriptionAccessImpact(
        current_state=current.state.radius_access_state,
        proposed_state=proposed.radius_access_state,
        allowed_before=current.state.radius_allowed,
        allowed_after=proposed.radius_allowed,
        session_action=_session_action(command.kind),
        block_reason_after=proposed.access_block_reason,
    )
    return SubscriptionLifecyclePreview(
        command=command,
        current=current,
        proposed=proposed,
        effective_at=effective_at,
        eligible=not reasons,
        eligibility_reasons=tuple(dict.fromkeys(reasons)),
        billing_impact=billing_impact,
        access_impact=access_impact,
    )


def _target_offer(
    db: Session,
    command: SubscriptionLifecycleCommand,
) -> CatalogOffer | None:
    if command.kind != SubscriptionCommandKind.change_plan:
        return None
    return db.get(CatalogOffer, coerce_uuid(command.target_offer_id))


def _eligibility_reasons(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    *,
    target_offer: CatalogOffer | None,
    now: datetime,
) -> list[str]:
    status = subscription.status
    reasons: list[str] = []
    allowed_statuses = {
        SubscriptionCommandKind.activate: {SubscriptionStatus.pending},
        SubscriptionCommandKind.suspend: {
            SubscriptionStatus.active,
            SubscriptionStatus.pending,
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
            SubscriptionStatus.stopped,
        },
        SubscriptionCommandKind.restore: {
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
            SubscriptionStatus.stopped,
        },
        SubscriptionCommandKind.renew: {
            SubscriptionStatus.active,
            SubscriptionStatus.blocked,
            SubscriptionStatus.suspended,
        },
        SubscriptionCommandKind.cancel: set(SubscriptionStatus)
        - set(TERMINAL_SERVICE_STATUSES),
        SubscriptionCommandKind.expire: set(SubscriptionStatus)
        - set(TERMINAL_SERVICE_STATUSES),
        SubscriptionCommandKind.change_plan: {
            SubscriptionStatus.pending,
            SubscriptionStatus.active,
            SubscriptionStatus.suspended,
        },
    }
    if status not in allowed_statuses[command.kind]:
        reasons.append(f"status_{status.value}_not_eligible_for_{command.kind.value}")
    if (
        command.effective_timing == SubscriptionEffectiveTiming.scheduled
        and command.effective_at is not None
        and (_aware_utc(command.effective_at) or now) < now
    ):
        reasons.append("effective_at_is_in_the_past")
    if command.kind != SubscriptionCommandKind.change_plan:
        return reasons
    if target_offer is None:
        reasons.append("target_offer_not_found")
        return reasons
    if str(target_offer.id) == str(subscription.offer_id):
        reasons.append("already_on_target_offer")
        return reasons
    if not target_offer.is_active or target_offer.status != OfferStatus.active:
        reasons.append("target_offer_inactive")
        return reasons
    try:
        from app.services.catalog.subscriptions import _validate_plan_change

        _validate_plan_change(db, subscription, str(target_offer.id))
    except HTTPException as exc:
        detail = exc.detail
        code = (
            str(detail.get("code"))
            if isinstance(detail, dict) and detail.get("code")
            else "plan_change_policy_rejected"
        )
        reasons.append(code)
    if _active_change_request(db, subscription.id) is not None:
        reasons.append("outstanding_plan_change_exists")
    return reasons


def _active_change_request(
    db: Session, subscription_id: object
) -> SubscriptionChangeRequest | None:
    return db.scalar(
        select(SubscriptionChangeRequest)
        .where(SubscriptionChangeRequest.subscription_id == subscription_id)
        .where(
            SubscriptionChangeRequest.status.in_(
                {
                    SubscriptionChangeStatus.pending,
                    SubscriptionChangeStatus.approved,
                }
            )
        )
        .where(SubscriptionChangeRequest.applied_at.is_(None))
        .where(SubscriptionChangeRequest.is_active.is_(True))
        .order_by(SubscriptionChangeRequest.effective_date.asc())
    )


def _billing_impact(
    db: Session,
    subscription: Subscription,
    command: SubscriptionLifecycleCommand,
    *,
    current: SubscriptionLifecycleSnapshot,
    proposed: SubscriptionLifecycleState,
    target_offer: CatalogOffer | None,
    effective_at: datetime,
    current_balance: Decimal | None,
) -> tuple[SubscriptionBillingImpact, str | None]:
    before = current.state.billing_collectible
    after = proposed.billing_collectible
    if command.kind == SubscriptionCommandKind.change_plan and target_offer is not None:
        if command.effective_timing != SubscriptionEffectiveTiming.immediate:
            currency, recurring = _recurring_price(db, target_offer.id)
            return (
                SubscriptionBillingImpact(
                    action="replace_recurring_price_at_effective_date",
                    collectible_before=before,
                    collectible_after=after,
                    currency=currency,
                    required_amount=recurring,
                    details={"target_recurring_amount": recurring},
                ),
                None,
            )
        from app.services.prepaid_plan_changes import resolve_prepaid_plan_change

        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            str(target_offer.id),
            # Preserve the pricing owner's clock unless the command carries an
            # explicit effective time. Tests, scheduled jobs, and billing runs
            # can freeze that owner independently of this presentation layer.
            effective_at=_aware_utc(command.effective_at),
            prepaid_funding_before=current_balance,
        )
        quote = decision.as_quote_dict()
        return (
            SubscriptionBillingImpact(
                action="apply_prorated_plan_change",
                collectible_before=before,
                collectible_after=after,
                currency=decision.currency,
                charge_amount=round_money(
                    Decimal(str(decision.proration.get("charge_amount", "0.00")))
                ),
                credit_amount=round_money(
                    Decimal(str(decision.proration.get("credit_amount", "0.00")))
                ),
                net_amount=decision.net_amount,
                available_balance=decision.prepaid_funding_before,
                required_amount=decision.required_amount,
                shortfall=decision.shortfall,
                collection_blocking_balance=decision.collection_blocking_balance,
                details={"quote": quote},
            ),
            decision.reason,
        )
    if command.kind == SubscriptionCommandKind.renew:
        currency, recurring = _recurring_price(db, subscription.offer_id)
        return (
            SubscriptionBillingImpact(
                action="billing_owned_renewal",
                collectible_before=before,
                collectible_after=after,
                currency=currency,
                required_amount=recurring,
                details={"next_billing_at": effective_at.isoformat()},
            ),
            None,
        )
    action = {
        SubscriptionCommandKind.activate: "start_or_resume_collection",
        SubscriptionCommandKind.suspend: "continue_collection_while_held",
        SubscriptionCommandKind.restore: "collection_unchanged",
        SubscriptionCommandKind.cancel: "stop_collection",
        SubscriptionCommandKind.expire: "stop_collection",
    }.get(command.kind, "collection_unchanged")
    return (
        SubscriptionBillingImpact(
            action=action,
            collectible_before=before,
            collectible_after=after,
            possible_prorated_credit=command.kind == SubscriptionCommandKind.cancel,
        ),
        None,
    )


def _resolve_state(
    subscription: Subscription,
    *,
    subscriber: object | None,
    status: SubscriptionStatus | None = None,
    offer: CatalogOffer | None = None,
) -> SubscriptionLifecycleState:
    projected_status = status or subscription.status
    projected_offer = offer or subscription.offer
    billing_mode = subscription.billing_mode
    if (
        projected_offer is not None
        and str(projected_offer.id) != str(subscription.offer_id)
        and projected_offer.billing_mode is not None
    ):
        billing_mode = projected_offer.billing_mode
    projected = SimpleNamespace(
        id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        subscriber=subscriber,
        status=projected_status,
        billing_mode=billing_mode,
    )
    access = resolve_customer_access(projected, subscriber=subscriber)
    return SubscriptionLifecycleState(
        status=projected_status.value,
        offer_id=str(projected_offer.id if projected_offer else subscription.offer_id),
        offer_name=projected_offer.name if projected_offer else None,
        billing_mode=_enum_value(billing_mode) or "",
        billing_collectible=(
            access.is_billable_account
            and projected_status in BILLING_COLLECTIBLE_SERVICE_STATUSES
        ),
        mrr_countable=(
            access.is_active_customer_service
            and projected_status in MRR_COUNTABLE_SERVICE_STATUSES
        ),
        radius_access_state=(
            access.radius_access_state.value if access.radius_access_state else None
        ),
        radius_allowed=access.radius_allowed,
        radius_blocked=access.radius_blocked,
        access_block_reason=access.access_block_reason,
        terminal=projected_status in TERMINAL_SERVICE_STATUSES,
    )


def _proposed_status(
    current: SubscriptionStatus, kind: SubscriptionCommandKind
) -> SubscriptionStatus:
    if (
        kind == SubscriptionCommandKind.suspend
        and current in _SUSPENDED_EQUIVALENT_STATUSES
    ):
        return current
    return {
        SubscriptionCommandKind.activate: SubscriptionStatus.active,
        SubscriptionCommandKind.suspend: SubscriptionStatus.suspended,
        SubscriptionCommandKind.restore: SubscriptionStatus.active,
        SubscriptionCommandKind.cancel: SubscriptionStatus.canceled,
        SubscriptionCommandKind.expire: SubscriptionStatus.expired,
    }.get(kind, current)


def _session_action(kind: SubscriptionCommandKind) -> SubscriptionSessionAction:
    return {
        SubscriptionCommandKind.activate: SubscriptionSessionAction.authorize,
        SubscriptionCommandKind.restore: SubscriptionSessionAction.authorize,
        SubscriptionCommandKind.suspend: SubscriptionSessionAction.disconnect,
        SubscriptionCommandKind.cancel: SubscriptionSessionAction.deprovision,
        SubscriptionCommandKind.expire: SubscriptionSessionAction.deprovision,
        SubscriptionCommandKind.change_plan: SubscriptionSessionAction.reauthorize,
    }.get(kind, SubscriptionSessionAction.none)


def _effective_at(
    current: SubscriptionLifecycleSnapshot,
    command: SubscriptionLifecycleCommand,
    *,
    now: datetime,
) -> datetime:
    if command.effective_at is not None:
        return _aware_utc(command.effective_at) or now
    if command.effective_timing == SubscriptionEffectiveTiming.next_cycle:
        return current.next_billing_at or now
    if command.kind == SubscriptionCommandKind.renew:
        return current.next_billing_at or now
    return now


def _recurring_price(db: Session, offer_id: object) -> tuple[str, Decimal]:
    row = db.execute(
        select(OfferPrice.currency, OfferPrice.amount)
        .where(OfferPrice.offer_id == offer_id)
        .where(OfferPrice.price_type == PriceType.recurring)
        .where(OfferPrice.is_active.is_(True))
        .order_by(OfferPrice.created_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return "NGN", Decimal("0.00")
    return str(row.currency or "NGN"), round_money(Decimal(str(row.amount)))


def _subscription_head(
    db: Session,
    subscription: Subscription,
    *,
    subscriber: object | None,
    pending_change: SubscriptionChangeRequest | None,
) -> str:
    currency, recurring_amount = _recurring_price(db, subscription.offer_id)
    offer = subscription.offer
    source = "|".join(
        (
            str(subscription.id),
            subscription.status.value,
            str(subscription.offer_id),
            _enum_value(subscription.billing_mode) or "missing-billing-mode",
            _head_datetime(subscription.start_at),
            _head_datetime(subscription.end_at),
            _head_datetime(subscription.next_billing_at),
            _head_datetime(subscription.canceled_at),
            _head_datetime(subscription.updated_at),
            _enum_value(getattr(subscriber, "status", None))
            or "missing-account-status",
            str(bool(subscriber and getattr(subscriber, "is_active", False))),
            _head_datetime(getattr(subscriber, "updated_at", None)),
            _enum_value(getattr(offer, "status", None)) or "missing-offer-status",
            str(bool(offer and offer.is_active)),
            _enum_value(getattr(offer, "billing_mode", None))
            or "missing-offer-billing-mode",
            _head_datetime(getattr(offer, "updated_at", None)),
            currency,
            str(recurring_amount),
            str(pending_change.id) if pending_change else "no-pending-change",
            (
                _enum_value(pending_change.status) or "missing-change-status"
                if pending_change
                else "no-pending-change-status"
            ),
            (
                str(pending_change.requested_offer_id)
                if pending_change
                else "no-pending-target"
            ),
            (
                pending_change.effective_date.isoformat()
                if pending_change
                else "no-pending-effective-date"
            ),
            _head_datetime(
                getattr(pending_change, "updated_at", None) if pending_change else None
            ),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _head_datetime(value: object | None) -> str:
    if not isinstance(value, datetime):
        return "missing"
    normalized = _aware_utc(value)
    return normalized.isoformat() if normalized else "missing"


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


__all__ = [
    "PendingSubscriptionChange",
    "SubscriptionAccessImpact",
    "SubscriptionBillingImpact",
    "SubscriptionCommandKind",
    "SubscriptionCommandOutcome",
    "SubscriptionCommandOutcomeStatus",
    "SubscriptionEffectiveTiming",
    "SubscriptionLifecycleCommand",
    "SubscriptionLifecycleError",
    "SubscriptionLifecycleHeadConflict",
    "SubscriptionLifecyclePreview",
    "SubscriptionLifecycleSnapshot",
    "SubscriptionLifecycleState",
    "SubscriptionSessionAction",
    "assert_subscription_head",
    "assert_subscription_transition",
    "preview_subscription_command",
    "resolve_subscription_lifecycle",
]
