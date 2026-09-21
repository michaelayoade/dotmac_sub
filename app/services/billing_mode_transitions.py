"""Reviewed, account-wide prepaid/postpaid billing-mode transitions."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import BillingMode, CatalogOffer, Subscription
from app.models.enforcement_lock import EnforcementLock
from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.offer_availability import OfferBillingModeAvailability
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_billing_treatment import (
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
)
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services.action_readiness import (
    ActionableBlocker,
    ActionReadiness,
    BlockerEvidence,
    ReadinessImpact,
    ReadinessState,
)
from app.services.audit_adapter import stage_audit_event
from app.services.billing_profile import (
    BillingProfileReason,
    resolve_billing_profile,
    resolve_offer_supported_billing_modes,
)
from app.services.customer_chargeability import (
    CHARGEABILITY_SERVICE_STATUSES,
    CustomerChargeabilityStatus,
    resolve_customer_chargeability,
)
from app.services.customer_financial_position import (
    get_customer_financial_position,
    get_native_customer_financial_balance,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_enforcement_state import clear_prepaid_enforcement_timers
from app.services.subscription_billing_treatments import (
    SubscriptionBillingTreatmentError,
    resolve_subscription_reference_price,
)

BILLING_MODE_WRITE_SCOPE = "billing:mode:write"
_OWNER = "financial.billing_mode_transition"
_IDEMPOTENCY_SCOPE = "billing_mode_transition"
_CONFIRM_COMMAND = OwnerCommandDefinition(
    owner=_OWNER,
    concern="account-wide billing-mode transition",
    name="confirm_billing_mode_transition",
)


class BillingModeTransitionIssue(StrEnum):
    account_not_approved = "account_not_approved"
    account_status_ineligible = "account_status_ineligible"
    billing_profile_invalid = "billing_profile_invalid"
    already_in_target_mode = "already_in_target_mode"
    subscription_mode_mismatch = "subscription_mode_mismatch"
    no_current_service = "no_current_service"
    pricing_review_required = "pricing_review_required"
    non_billable_service = "non_billable_service"
    target_mode_unavailable = "target_mode_unavailable"
    billing_treatment_open = "billing_treatment_open"
    pending_plan_change = "pending_plan_change"
    active_enforcement_lock = "active_enforcement_lock"
    draft_invoice_open = "draft_invoice_open"
    billing_anchor_missing = "billing_anchor_missing"
    price_evidence_invalid = "price_evidence_invalid"
    price_currency_mismatch = "price_currency_mismatch"
    prepaid_funding_insufficient = "prepaid_funding_insufficient"
    existing_receivable_preserved = "existing_receivable_preserved"


class BillingModeTransitionError(DomainError):
    """Stable transport-neutral billing-mode transition failure."""


def _error(
    suffix: str,
    message: str,
    **details: object,
) -> BillingModeTransitionError:
    return BillingModeTransitionError(
        code=f"{_OWNER}.{suffix}",
        message=message,
        details=details,
    )


@dataclass(frozen=True, slots=True)
class BillingModeSubscriptionImpact:
    subscription_id: UUID
    offer_id: UUID
    current_mode: BillingMode
    target_mode: BillingMode
    supported_modes: frozenset[BillingMode]
    next_billing_at: datetime | None
    recurring_amount: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class PreviewBillingModeTransitionRequest:
    account_id: UUID
    target_mode: BillingMode
    evaluated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BillingModeTransitionPreview:
    account_id: UUID
    current_mode: BillingMode | None
    target_mode: BillingMode
    account_status: SubscriberStatus
    subscriptions: tuple[BillingModeSubscriptionImpact, ...]
    first_target_billing_at: datetime | None
    required_prepaid_funding: Decimal
    available_credit: Decimal
    outstanding_receivables: Decimal
    currency: str | None
    readiness: ActionReadiness
    fingerprint: str

    @property
    def allowed(self) -> bool:
        return self.readiness.is_ready


@dataclass(frozen=True, slots=True)
class ConfirmBillingModeTransitionCommand:
    context: CommandContext
    account_id: UUID
    target_mode: BillingMode
    expected_preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class BillingModeTransitionOutcome:
    account_id: UUID
    prior_mode: BillingMode
    billing_mode: BillingMode
    changed_subscription_ids: tuple[UUID, ...]
    first_target_billing_at: datetime | None
    replayed: bool


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


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _blocker(
    issue: BillingModeTransitionIssue,
    *,
    owner: str,
    message: str,
    detail: str,
    observed_at: datetime,
    evidence: str,
    advisory: bool = False,
) -> ActionableBlocker:
    return ActionableBlocker(
        code=issue.value,
        owner=owner,
        customer_message=message,
        staff_detail=detail,
        evidence=BlockerEvidence(
            summary=evidence,
            observed_at=observed_at,
            source=owner,
        ),
        impact=(ReadinessImpact.advisory if advisory else ReadinessImpact.blocking),
    )


def _load_account(db: Session, account_id: UUID) -> Subscriber:
    account = db.get(Subscriber, account_id)
    if account is None:
        raise _error(
            "account_not_found",
            "The billing account was not found.",
            account_id=str(account_id),
        )
    return account


def _transition_subscriptions(db: Session, account_id: UUID) -> list[Subscription]:
    return list(
        db.scalars(
            select(Subscription)
            .where(
                Subscription.subscriber_id == account_id,
                Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
            )
            .order_by(Subscription.id)
        ).all()
    )


def _preview(
    db: Session,
    *,
    account: Subscriber,
    target_mode: BillingMode,
    evaluated_at: datetime,
) -> BillingModeTransitionPreview:
    profile = resolve_billing_profile(db, account)
    current_mode = profile.effective_mode
    subscriptions = _transition_subscriptions(db, account.id)
    subscription_ids = tuple(item.id for item in subscriptions)
    blockers: list[ActionableBlocker] = []

    if not account.billing_enabled:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.account_not_approved,
                owner="customer.billing_approval",
                message="The account is not approved for active billing service.",
                detail="Reapprove the account through the billing-approval owner first.",
                observed_at=evaluated_at,
                evidence="Subscriber.billing_enabled is false.",
            )
        )
    if account.status in {
        SubscriberStatus.new,
        SubscriberStatus.disabled,
        SubscriberStatus.canceled,
    }:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.account_status_ineligible,
                owner="access.subscription_lifecycle",
                message="The account lifecycle does not permit a billing-mode change.",
                detail=f"Account status {account.status.value} is not eligible.",
                observed_at=evaluated_at,
                evidence=f"Account status is {account.status.value}.",
            )
        )
    if not profile.automation_safe:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.billing_profile_invalid,
                owner="financial.billing_profile",
                message="The account and subscription billing modes must be repaired first.",
                detail=(
                    profile.invalid_reason.value
                    if profile.invalid_reason is not None
                    else BillingProfileReason.ACCOUNT_SUBSCRIPTION_BILLING_MODE_MISMATCH.value
                ),
                observed_at=evaluated_at,
                evidence="The canonical billing profile is not automation-safe.",
            )
        )
    if current_mode == target_mode and profile.automation_safe:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.already_in_target_mode,
                owner="financial.billing_profile",
                message="The account already uses the selected billing mode.",
                detail="Choose the opposite billing mode to perform a conversion.",
                observed_at=evaluated_at,
                evidence=f"Effective billing mode is already {target_mode.value}.",
            )
        )

    modes = {item.billing_mode for item in subscriptions}
    if current_mode is not None and (
        len(modes) > 1 or any(mode != current_mode for mode in modes)
    ):
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.subscription_mode_mismatch,
                owner="financial.billing_profile",
                message="Current services do not share one billing mode.",
                detail="Repair the account/subscription billing profile before conversion.",
                observed_at=evaluated_at,
                evidence="Transition-scope subscription modes are inconsistent.",
            )
        )

    chargeability = resolve_customer_chargeability(db, (account.id,))[account.id]
    if chargeability.status is CustomerChargeabilityStatus.no_current_service:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.no_current_service,
                owner="access.subscription_lifecycle",
                message="The account has no current service to convert.",
                detail="Add or restore an eligible service before changing billing mode.",
                observed_at=evaluated_at,
                evidence="No current subscription is in the transition scope.",
            )
        )
    elif chargeability.status is CustomerChargeabilityStatus.review_required:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.pricing_review_required,
                owner="service_intent.catalog_policy",
                message="Pricing must be reviewed before changing billing mode.",
                detail="Correct missing, multiple, or contradictory recurring prices.",
                observed_at=evaluated_at,
                evidence=", ".join(reason.value for reason in chargeability.reasons),
            )
        )
    elif chargeability.status is CustomerChargeabilityStatus.confirmed_non_billable:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.non_billable_service,
                owner="financial.subscription_billing_treatments",
                message="Non-billable service requires a separate commercial review.",
                detail="Remove or complete protected treatment/free-service handling first.",
                observed_at=evaluated_at,
                evidence="The complete current service scope is non-billable.",
            )
        )

    open_treatment_ids = set(
        db.scalars(
            select(SubscriptionBillingArrangement.subscription_id).where(
                SubscriptionBillingArrangement.subscription_id.in_(subscription_ids),
                SubscriptionBillingArrangement.status == BillingTreatmentStatus.active,
                SubscriptionBillingArrangement.ends_at > evaluated_at,
            )
        ).all()
        if subscription_ids
        else ()
    )
    if open_treatment_ids:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.billing_treatment_open,
                owner="financial.subscription_billing_treatments",
                message="An open billing treatment protects one or more services.",
                detail="Revoke or complete the treatment before changing commercial terms.",
                observed_at=evaluated_at,
                evidence=f"{len(open_treatment_ids)} subscription treatment(s) are open.",
            )
        )

    pending_change_ids = set(
        db.scalars(
            select(SubscriptionChangeRequest.subscription_id).where(
                SubscriptionChangeRequest.subscription_id.in_(subscription_ids),
                SubscriptionChangeRequest.status == SubscriptionChangeStatus.pending,
            )
        ).all()
        if subscription_ids
        else ()
    )
    if pending_change_ids:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.pending_plan_change,
                owner="service_intent.subscription_change_execution",
                message="A pending plan change must be completed or canceled first.",
                detail="Billing mode cannot change while commercial service terms are pending.",
                observed_at=evaluated_at,
                evidence=f"{len(pending_change_ids)} subscription(s) have pending changes.",
            )
        )

    active_lock_count = int(
        db.scalar(
            select(EnforcementLock.id)
            .where(
                EnforcementLock.subscriber_id == account.id,
                EnforcementLock.is_active.is_(True),
            )
            .limit(1)
        )
        is not None
    )
    if active_lock_count:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.active_enforcement_lock,
                owner="access.subscription_lifecycle",
                message="Active service restrictions must be resolved before conversion.",
                detail="The conversion never clears unrelated enforcement locks.",
                observed_at=evaluated_at,
                evidence="At least one active enforcement lock exists.",
            )
        )

    draft_invoice_count = int(
        db.scalar(
            select(Invoice.id)
            .where(
                Invoice.account_id == account.id,
                Invoice.is_active.is_(True),
                Invoice.status == InvoiceStatus.draft,
            )
            .limit(1)
        )
        is not None
    )
    if draft_invoice_count:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.draft_invoice_open,
                owner="financial.invoices",
                message="Draft billing must be resolved before conversion.",
                detail="Issue, void, or correct open draft invoices before conversion.",
                observed_at=evaluated_at,
                evidence="At least one active draft invoice exists.",
            )
        )

    impacts: list[BillingModeSubscriptionImpact] = []
    unsupported_ids: list[UUID] = []
    missing_anchor_ids: list[UUID] = []
    invalid_price_ids: list[UUID] = []
    currencies: set[str] = set()
    due_prepaid_amount = Decimal("0.00")
    for subscription in subscriptions:
        offer = db.get(CatalogOffer, subscription.offer_id)
        supported_modes = (
            resolve_offer_supported_billing_modes(db, offer)
            if offer is not None
            else frozenset()
        )
        if target_mode not in supported_modes:
            unsupported_ids.append(subscription.id)
        anchor = _utc(subscription.next_billing_at)
        if anchor is None:
            missing_anchor_ids.append(subscription.id)
        amount: Decimal | None = None
        currency: str | None = None
        try:
            price = resolve_subscription_reference_price(
                db,
                subscription,
                effective_at=evaluated_at,
            )
        except SubscriptionBillingTreatmentError:
            invalid_price_ids.append(subscription.id)
        else:
            amount = price.amount
            currency = price.currency
            currencies.add(currency)
            if amount <= 0:
                invalid_price_ids.append(subscription.id)
            elif (
                target_mode is BillingMode.prepaid
                and anchor is not None
                and anchor <= evaluated_at
            ):
                due_prepaid_amount += amount
        impacts.append(
            BillingModeSubscriptionImpact(
                subscription_id=subscription.id,
                offer_id=subscription.offer_id,
                current_mode=subscription.billing_mode,
                target_mode=target_mode,
                supported_modes=supported_modes,
                next_billing_at=anchor,
                recurring_amount=amount,
                currency=currency,
            )
        )
    if unsupported_ids:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.target_mode_unavailable,
                owner="service_intent.catalog_policy",
                message="One or more packages do not support the target billing mode.",
                detail="Add an active billing-mode availability variant or choose another package.",
                observed_at=evaluated_at,
                evidence=f"{len(unsupported_ids)} subscription offer(s) are unsupported.",
            )
        )
    if missing_anchor_ids:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.billing_anchor_missing,
                owner="financial.billing_profile",
                message="A reliable billing boundary is missing.",
                detail="Repair next_billing_at before conversion to prevent period overlap.",
                observed_at=evaluated_at,
                evidence=f"{len(missing_anchor_ids)} subscription(s) have no billing anchor.",
            )
        )
    if invalid_price_ids:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.price_evidence_invalid,
                owner="service_intent.catalog_policy",
                message="Positive recurring price evidence is incomplete or invalid.",
                detail="Repair contracted recurring prices before conversion.",
                observed_at=evaluated_at,
                evidence=f"{len(invalid_price_ids)} subscription price(s) are unresolved.",
            )
        )
    if len(currencies) > 1:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.price_currency_mismatch,
                owner="service_intent.catalog_policy",
                message="The account has recurring prices in multiple currencies.",
                detail="A single account-wide mode change cannot compare unlike currencies.",
                observed_at=evaluated_at,
                evidence=", ".join(sorted(currencies)),
            )
        )

    available_credit = Decimal("0.00")
    if target_mode is BillingMode.prepaid and len(currencies) == 1:
        currency = next(iter(currencies))
        balance = get_native_customer_financial_balance(
            db,
            account.id,
            currency=currency,
        )
        if balance.automation_safe:
            available_credit = max(balance.available_balance, Decimal("0.00"))
        if due_prepaid_amount > available_credit:
            blockers.append(
                _blocker(
                    BillingModeTransitionIssue.prepaid_funding_insufficient,
                    owner="customer.financial_position",
                    message="Sufficient prepaid credit is required at the conversion boundary.",
                    detail="Record funding before confirming the postpaid-to-prepaid conversion.",
                    observed_at=evaluated_at,
                    evidence=(
                        f"Required {due_prepaid_amount} {currency}; "
                        f"available {available_credit} {currency}."
                    ),
                )
            )

    financial_position = get_customer_financial_position(
        db,
        account.id,
        now=evaluated_at,
        include_prepaid_balance=False,
    )
    display_currency = (
        next(iter(currencies))
        if len(currencies) == 1
        else (financial_position.currency if not currencies else None)
    )
    if financial_position.open_invoice_balance > 0:
        blockers.append(
            _blocker(
                BillingModeTransitionIssue.existing_receivable_preserved,
                owner="financial.invoices",
                message="Existing invoices remain payable after conversion.",
                detail="The mode change does not erase or rewrite finalized receivables.",
                observed_at=evaluated_at,
                evidence=(
                    f"Outstanding receivables: {financial_position.open_invoice_balance} "
                    f"{financial_position.currency}."
                ),
                advisory=True,
            )
        )

    blocking = any(item.impact is ReadinessImpact.blocking for item in blockers)
    readiness = ActionReadiness(
        action_key="change_billing_mode",
        subject_type="subscriber",
        subject_id=str(account.id),
        owner=_OWNER,
        state=ReadinessState.blocked if blocking else ReadinessState.ready,
        evaluated_at=evaluated_at,
        blockers=tuple(blockers),
    )
    first_target_billing_at = min(
        (item.next_billing_at for item in impacts if item.next_billing_at is not None),
        default=None,
    )
    snapshot = {
        "account_id": str(account.id),
        "billing_enabled": bool(account.billing_enabled),
        "account_status": account.status.value,
        "current_mode": current_mode.value if current_mode else None,
        "target_mode": target_mode.value,
        "subscriptions": [
            {
                "id": str(item.subscription_id),
                "offer_id": str(item.offer_id),
                "current_mode": item.current_mode.value,
                "target_mode": item.target_mode.value,
                "supported_modes": sorted(mode.value for mode in item.supported_modes),
                "next_billing_at": (
                    item.next_billing_at.isoformat() if item.next_billing_at else None
                ),
                "recurring_amount": (
                    str(item.recurring_amount)
                    if item.recurring_amount is not None
                    else None
                ),
                "currency": item.currency,
            }
            for item in impacts
        ],
        "required_prepaid_funding": str(due_prepaid_amount),
        "available_credit": str(available_credit),
        "outstanding_receivables": str(financial_position.open_invoice_balance),
        "currency": display_currency,
        "blockers": [
            {"code": item.code, "impact": item.impact.value} for item in blockers
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BillingModeTransitionPreview(
        account_id=account.id,
        current_mode=current_mode,
        target_mode=target_mode,
        account_status=account.status,
        subscriptions=tuple(impacts),
        first_target_billing_at=first_target_billing_at,
        required_prepaid_funding=due_prepaid_amount,
        available_credit=available_credit,
        outstanding_receivables=financial_position.open_invoice_balance,
        currency=display_currency,
        readiness=readiness,
        fingerprint=fingerprint,
    )


def preview_billing_mode_transition(
    db: Session,
    request: PreviewBillingModeTransitionRequest,
) -> BillingModeTransitionPreview:
    evaluated_at = _utc(request.evaluated_at) or datetime.now(UTC)
    return _preview(
        db,
        account=_load_account(db, request.account_id),
        target_mode=request.target_mode,
        evaluated_at=evaluated_at,
    )


def _lock_transition_scope(db: Session, account_id: UUID) -> Subscriber:
    account = db.scalar(
        select(Subscriber).where(Subscriber.id == account_id).with_for_update()
    )
    if account is None:
        raise _error("account_not_found", "The billing account was not found.")
    subscription_ids = tuple(
        db.scalars(
            select(Subscription.id)
            .where(
                Subscription.subscriber_id == account_id,
                Subscription.status.in_(CHARGEABILITY_SERVICE_STATUSES),
            )
            .order_by(Subscription.id)
            .with_for_update()
        ).all()
    )
    if subscription_ids:
        offer_ids = tuple(
            db.scalars(
                select(Subscription.offer_id)
                .where(Subscription.id.in_(subscription_ids))
                .order_by(Subscription.offer_id)
            ).all()
        )
        if offer_ids:
            list(
                db.scalars(
                    select(CatalogOffer.id)
                    .where(CatalogOffer.id.in_(offer_ids))
                    .order_by(CatalogOffer.id)
                    .with_for_update()
                ).all()
            )
            list(
                db.scalars(
                    select(OfferBillingModeAvailability.id)
                    .where(OfferBillingModeAvailability.offer_id.in_(offer_ids))
                    .order_by(OfferBillingModeAvailability.id)
                    .with_for_update()
                ).all()
            )
        list(
            db.scalars(
                select(SubscriptionBillingArrangement.id)
                .where(
                    SubscriptionBillingArrangement.subscription_id.in_(subscription_ids)
                )
                .order_by(SubscriptionBillingArrangement.id)
                .with_for_update()
            ).all()
        )
        list(
            db.scalars(
                select(SubscriptionChangeRequest.id)
                .where(SubscriptionChangeRequest.subscription_id.in_(subscription_ids))
                .order_by(SubscriptionChangeRequest.id)
                .with_for_update()
            ).all()
        )
    list(
        db.scalars(
            select(EnforcementLock.id)
            .where(EnforcementLock.subscriber_id == account_id)
            .order_by(EnforcementLock.id)
            .with_for_update()
        ).all()
    )
    list(
        db.scalars(
            select(Invoice.id)
            .where(Invoice.account_id == account_id, Invoice.is_active.is_(True))
            .order_by(Invoice.id)
            .with_for_update()
        ).all()
    )
    return account


def _reserve_idempotency(
    db: Session,
    *,
    command: ConfirmBillingModeTransitionCommand,
) -> IdempotencyKey:
    key = str(command.context.idempotency_key or "").strip()
    if len(key) < 16 or len(key) > 120:
        raise _error(
            "invalid_idempotency_key",
            "A billing-mode idempotency key containing 16-120 characters is required.",
        )
    scope = f"{_IDEMPOTENCY_SCOPE}:{command.target_mode.value}"
    existing = db.scalar(
        select(IdempotencyKey)
        .where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
        .with_for_update()
    )
    if existing is not None:
        if existing.account_id != command.account_id:
            raise _error(
                "idempotency_account_mismatch",
                "The billing-mode confirmation belongs to another account.",
            )
        return existing
    reservation = IdempotencyKey(
        scope=scope,
        key=key,
        account_id=command.account_id,
    )
    db.add(reservation)
    try:
        db.flush()
    except IntegrityError as exc:
        raise _error(
            "idempotency_conflict",
            "The billing-mode confirmation conflicted with another request.",
        ) from exc
    return reservation


def _replayed(
    db: Session,
    *,
    account: Subscriber,
    target_mode: BillingMode,
    ref_id: str,
) -> BillingModeTransitionOutcome:
    try:
        event_id = UUID(ref_id)
    except ValueError as exc:
        raise _error(
            "invalid_replay_evidence",
            "Stored billing-mode replay evidence is invalid.",
        ) from exc
    event = db.scalar(
        select(EventStore).where(
            EventStore.event_id == event_id,
            EventStore.event_type == EventType.subscriber_billing_mode_changed.value,
            EventStore.account_id == account.id,
        )
    )
    payload = event.payload if event is not None else {}
    if (
        payload.get("account_id") != str(account.id)
        or payload.get("billing_mode") != target_mode.value
    ):
        raise _error(
            "invalid_replay_evidence",
            "Stored billing-mode replay evidence is invalid.",
        )
    try:
        prior_mode = BillingMode(str(payload["prior_mode"]))
        changed_ids = tuple(
            UUID(str(value)) for value in payload.get("changed_subscription_ids", ())
        )
        first_target_billing_at = (
            datetime.fromisoformat(str(payload["first_target_billing_at"]))
            if payload.get("first_target_billing_at")
            else None
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _error(
            "invalid_replay_evidence",
            "Stored billing-mode replay evidence is invalid.",
        ) from exc
    return BillingModeTransitionOutcome(
        account_id=account.id,
        prior_mode=prior_mode,
        billing_mode=target_mode,
        changed_subscription_ids=changed_ids,
        first_target_billing_at=_utc(first_target_billing_at),
        replayed=True,
    )


def _validate_command(command: ConfirmBillingModeTransitionCommand) -> None:
    if command.context.scope != BILLING_MODE_WRITE_SCOPE:
        raise _error(
            "invalid_scope",
            "Billing-mode write permission is required.",
        )
    if not command.context.reason.strip():
        raise _error("invalid_reason", "A billing-mode change reason is required.")


def confirm_billing_mode_transition(
    db: Session,
    command: ConfirmBillingModeTransitionCommand,
) -> BillingModeTransitionOutcome:
    """Confirm a reviewed account-wide billing-mode change exactly once."""

    def operation() -> BillingModeTransitionOutcome:
        _validate_command(command)
        account = _lock_transition_scope(db, command.account_id)
        reservation = _reserve_idempotency(db, command=command)
        if reservation.ref_id:
            return _replayed(
                db,
                account=account,
                target_mode=command.target_mode,
                ref_id=reservation.ref_id,
            )

        preview = _preview(
            db,
            account=account,
            target_mode=command.target_mode,
            evaluated_at=datetime.now(UTC),
        )
        expected = command.expected_preview_fingerprint.strip()
        if len(expected) != 64:
            raise _error(
                "invalid_preview_fingerprint",
                "The billing-mode preview fingerprint is invalid.",
            )
        if not secrets.compare_digest(preview.fingerprint, expected):
            raise _error(
                "stale_preview",
                "Billing evidence changed after preview; review the conversion again.",
            )
        if not preview.allowed or preview.current_mode is None:
            raise _error(
                "transition_not_allowed",
                "The billing-mode transition is not currently allowed.",
                blocker_codes=[
                    item.code for item in preview.readiness.blocking_blockers
                ],
            )

        prior_mode = preview.current_mode
        changed_ids: list[UUID] = []
        subscriptions = _transition_subscriptions(db, account.id)
        account.billing_mode = command.target_mode
        for subscription in subscriptions:
            if subscription.billing_mode == command.target_mode:
                continue
            subscription.billing_mode = command.target_mode
            changed_ids.append(subscription.id)
        if (
            prior_mode is BillingMode.prepaid
            and command.target_mode is BillingMode.postpaid
        ):
            clear_prepaid_enforcement_timers(db, account.id)
        db.flush()

        actor_type, actor_id = _actor(command.context)
        metadata: dict[str, object] = {
            "schema_version": 1,
            "account_id": str(account.id),
            "prior_mode": prior_mode.value,
            "billing_mode": command.target_mode.value,
            "changed_subscription_ids": [str(item) for item in changed_ids],
            "first_target_billing_at": (
                preview.first_target_billing_at.isoformat()
                if preview.first_target_billing_at
                else None
            ),
            "required_prepaid_funding": str(preview.required_prepaid_funding),
            "available_credit": str(preview.available_credit),
            "outstanding_receivables": str(preview.outstanding_receivables),
            "reason": command.context.reason,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "preview_fingerprint": preview.fingerprint,
        }
        stage_audit_event(
            db,
            action="billing.account_mode_changed",
            entity_type="subscriber",
            entity_id=str(account.id),
            actor_type=actor_type,
            actor_id=actor_id,
            request_id=str(command.context.correlation_id),
            metadata=metadata,
        )
        event = emit_event(
            db,
            EventType.subscriber_billing_mode_changed,
            metadata,
            actor=command.context.actor,
            subscriber_id=account.id,
            account_id=account.id,
        )
        reservation.ref_id = str(event.event_id)
        db.flush()
        return BillingModeTransitionOutcome(
            account_id=account.id,
            prior_mode=prior_mode,
            billing_mode=command.target_mode,
            changed_subscription_ids=tuple(changed_ids),
            first_target_billing_at=preview.first_target_billing_at,
            replayed=False,
        )

    return execute_owner_command(
        db,
        definition=_CONFIRM_COMMAND,
        context=command.context,
        operation=operation,
    )


__all__ = [
    "BILLING_MODE_WRITE_SCOPE",
    "BillingModeSubscriptionImpact",
    "BillingModeTransitionError",
    "BillingModeTransitionIssue",
    "BillingModeTransitionOutcome",
    "BillingModeTransitionPreview",
    "ConfirmBillingModeTransitionCommand",
    "PreviewBillingModeTransitionRequest",
    "confirm_billing_mode_transition",
    "preview_billing_mode_transition",
]
