"""Owner for funding one due prepaid service period from customer position.

Payment settlement records confirmed money and emits a funding-change event;
it never creates service debit or entitlement evidence. This owner handles both
payment-triggered and scheduled renewal decisions. It posts one preview-bound
account adjustment, links one active service entitlement to that exact debit,
and advances the subscription anchor in the caller's transaction.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.schemas.billing import AccountAdjustmentPreviewRequest
from app.services.billing._common import lock_account
from app.services.billing.adjustments import (
    ACCOUNT_ADJUSTMENT_SCOPE,
    AccountAdjustmentError,
    AccountAdjustmentOrigin,
    PreviewAccountAdjustmentQuery,
    StageSystemAccountAdjustmentCommand,
    preview_account_adjustment,
    stage_system_account_adjustment,
)
from app.services.common import coerce_uuid, round_money
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    prepaid_entitlement_coverage_end,
)

_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
    }
)
_MAX_AUTOMATIC_LAG = timedelta(days=2)


class PrepaidServiceRenewalError(DomainError):
    """Transport-neutral renewal failure."""


def _error(suffix: str, message: str, **details: object) -> NoReturn:
    raise PrepaidServiceRenewalError(
        code=f"financial.prepaid_service_renewals.{suffix}",
        message=message,
        details=details,
    )


def _adjustment_error(exc: AccountAdjustmentError) -> NoReturn:
    _error(
        "adjustment_rejected",
        exc.message,
        account_adjustment_code=exc.code,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _origin_ref(subscription_id: object, starts_at: datetime, ends_at: datetime) -> str:
    return f"{subscription_id}:{starts_at.isoformat()}:{ends_at.isoformat()}"


def _idempotency_key(origin_ref: str) -> str:
    return "prepaid-renewal-" + hashlib.sha256(origin_ref.encode("utf-8")).hexdigest()


def resolve_prepaid_monthly_charge(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> tuple[Decimal, str, BillingCycle] | None:
    """Resolve the one canonical taxed monthly renewal amount."""
    from app.models.billing import TaxApplication, TaxRate
    from app.services.billing._common import _calculate_tax_amount
    from app.services.billing_automation import (
        _default_tax_application,
        _effective_unit_price,
        _resolve_price,
        _resolve_tax_rate_id,
    )

    # A recurring prepaid debit consumes the customer's money immediately, so
    # the contracted price must be structural evidence on the subscription.
    # Falling back to the current catalog price can turn an incomplete import
    # into an overcharge (the retained source has negotiated prices far below
    # today's offer price). Missing/zero terms are an operator blocker, not a
    # pricing decision for the renewal owner.
    if subscription.unit_price is None or subscription.unit_price <= 0:
        return None
    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        return None
    effective_cycle = cycle or BillingCycle.monthly
    if effective_cycle != BillingCycle.monthly:
        return None
    base = _effective_unit_price(subscription, amount, effective_at)
    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    if not tax_rate_id:
        return base, currency or "NGN", effective_cycle
    tax_rate = db.get(TaxRate, tax_rate_id)
    if tax_rate is None:
        return base, currency or "NGN", effective_cycle
    tax_application = _default_tax_application(db)
    tax_amount = _calculate_tax_amount(
        base, Decimal(str(tax_rate.rate)), tax_application
    )
    total = (
        base
        if tax_application == TaxApplication.inclusive
        else round_money(base + tax_amount)
    )
    return total, currency or "NGN", effective_cycle


@dataclass(frozen=True)
class PrepaidServiceRenewalPreview:
    account_id: UUID
    subscription_id: UUID
    starts_at: datetime
    ends_at: datetime
    amount: Decimal
    currency: str
    funding_before: Decimal
    funding_after: Decimal
    shortfall: Decimal
    allowed: bool
    fingerprint: str
    idempotency_key: str
    origin_ref: str
    replayed: bool = False


@dataclass(frozen=True)
class PrepaidServiceRenewalResult:
    preview: PrepaidServiceRenewalPreview
    adjustment: AccountAdjustment
    ledger_entry: LedgerEntry
    entitlement: ServiceEntitlement
    replayed: bool


class PrepaidServiceRenewalSource(enum.StrEnum):
    direct_payment = "direct_payment"
    account_credit = "account_credit"
    scheduled = "scheduled"


@dataclass(frozen=True)
class PrepaidServiceRenewedOutcome:
    """Exact customer-visible result of one forward prepaid renewal."""

    event_id: UUID
    account_id: UUID
    subscription_id: UUID
    entitlement_id: UUID
    ledger_entry_id: UUID
    period_start: datetime
    renewed_through: datetime
    amount: Decimal
    currency: str
    source: PrepaidServiceRenewalSource
    trigger_payment_id: UUID | None = None


class FundingChangeRenewalDisposition(enum.StrEnum):
    no_due_service = "no_due_service"
    payable_invoice_remaining = "payable_invoice_remaining"
    funded = "funded"
    unfunded = "unfunded"
    already_covered = "already_covered"
    missing_price = "missing_price"
    currency_mismatch = "currency_mismatch"


@dataclass(frozen=True)
class FundingChangeRenewalResult:
    account_id: UUID
    scanned: int
    funded: int
    unfunded: int
    already_covered: int
    missing_price: int
    currency_mismatch: int
    disposition: FundingChangeRenewalDisposition
    renewals: tuple[PrepaidServiceRenewedOutcome, ...] = ()


def _subscription_for_request(
    db: Session,
    subscription_id: object,
) -> Subscription:
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if subscription is None:
        _error("subscription_not_found", "Subscription was not found.")
    if subscription.billing_mode != BillingMode.prepaid:
        _error(
            "ineligible_billing_mode",
            "Only a prepaid subscription can receive a funded service cycle.",
        )
    if subscription.status not in PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES:
        _error(
            "ineligible_status",
            "Subscription is not eligible for prepaid service renewal.",
        )
    return subscription


def _existing_period_entitlement(
    db: Session,
    *,
    subscription_id: object,
    starts_at: datetime,
    ends_at: datetime,
) -> ServiceEntitlement | None:
    return db.scalar(
        select(ServiceEntitlement).where(
            ServiceEntitlement.subscription_id == subscription_id,
            ServiceEntitlement.status == ServiceEntitlementStatus.active,
            ServiceEntitlement.starts_at < ends_at,
            ServiceEntitlement.ends_at > starts_at,
        )
    )


def preview_prepaid_service_renewal(
    db: Session,
    *,
    subscription_id: object,
    starts_at: datetime,
    ends_at: datetime,
    amount: Decimal,
    currency: str = "NGN",
) -> PrepaidServiceRenewalPreview:
    subscription = _subscription_for_request(db, subscription_id)
    period_start = _utc(starts_at)
    period_end = _utc(ends_at)
    if period_end <= period_start:
        _error("invalid_period", "Renewal period must be positive.")
    charge = round_money(amount)
    if charge <= Decimal("0.00"):
        _error("invalid_amount", "Renewal amount must be positive.")
    unit = str(currency).strip().upper()
    if len(unit) != 3:
        _error("invalid_currency", "Renewal currency is invalid.")

    origin_ref = _origin_ref(subscription.id, period_start, period_end)
    idempotency_key = _idempotency_key(origin_ref)
    overlap = _existing_period_entitlement(
        db,
        subscription_id=subscription.id,
        starts_at=period_start,
        ends_at=period_end,
    )
    if overlap is not None:
        existing_adjustment = db.scalar(
            select(AccountAdjustment).where(
                AccountAdjustment.origin == _ORIGIN,
                AccountAdjustment.idempotency_key == idempotency_key,
            )
        )
        if (
            existing_adjustment is not None
            and overlap.source_ledger_entry_id == existing_adjustment.ledger_entry_id
            and overlap.account_id == subscription.subscriber_id
            and _utc(overlap.starts_at) == period_start
            and _utc(overlap.ends_at) == period_end
            and round_money(overlap.amount_funded) == charge
            and overlap.currency == unit
        ):
            return PrepaidServiceRenewalPreview(
                account_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                starts_at=period_start,
                ends_at=period_end,
                amount=charge,
                currency=unit,
                funding_before=round_money(existing_adjustment.prepaid_funding_before),
                funding_after=round_money(existing_adjustment.prepaid_funding_after),
                shortfall=Decimal("0.00"),
                allowed=True,
                fingerprint=existing_adjustment.preview_fingerprint,
                idempotency_key=idempotency_key,
                origin_ref=origin_ref,
                replayed=True,
            )
        _error(
            "period_already_funded",
            "Prepaid service period already has active funding evidence.",
        )

    try:
        adjustment_preview = preview_account_adjustment(
            db,
            PreviewAccountAdjustmentQuery(
                request=AccountAdjustmentPreviewRequest(
                    account_id=subscription.subscriber_id,
                    category=LedgerCategory.internet_service,
                    amount=charge,
                    currency=unit,
                    memo=(
                        "Prepaid service renewal "
                        f"{period_start.date()} - {period_end.date()}"
                    ),
                    reason="Funded prepaid service period",
                ),
                origin=_ORIGIN,
                origin_ref=origin_ref,
            ),
        )
    except AccountAdjustmentError as exc:
        _adjustment_error(exc)
    return PrepaidServiceRenewalPreview(
        account_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        starts_at=period_start,
        ends_at=period_end,
        amount=charge,
        currency=unit,
        funding_before=adjustment_preview.prepaid_funding_before,
        funding_after=adjustment_preview.prepaid_funding_after,
        shortfall=adjustment_preview.shortfall,
        allowed=adjustment_preview.allowed,
        fingerprint=adjustment_preview.fingerprint,
        idempotency_key=idempotency_key,
        origin_ref=origin_ref,
    )


def confirm_prepaid_service_renewal(
    db: Session,
    preview: PrepaidServiceRenewalPreview,
    *,
    evidence_ref: str,
) -> PrepaidServiceRenewalResult:
    """Lock, re-preview, and atomically stage debit + entitlement + anchor."""
    evidence = evidence_ref.strip()
    if not evidence:
        _error("missing_evidence_ref", "An evidence reference is required.")

    # Serialize the idempotency lookup with the funding re-preview and write.
    # Looking up the adjustment before this lock let two concurrent callers
    # both observe "missing"; the second caller then re-previewed after the
    # first committed and failed with a stale fingerprint instead of returning
    # the already-recorded renewal.
    lock_account(db, str(preview.account_id))
    existing_adjustment = db.scalar(
        select(AccountAdjustment).where(
            AccountAdjustment.origin == _ORIGIN,
            AccountAdjustment.idempotency_key == preview.idempotency_key,
        )
    )
    if existing_adjustment is not None:
        entitlement = db.scalar(
            select(ServiceEntitlement).where(
                ServiceEntitlement.source_ledger_entry_id
                == existing_adjustment.ledger_entry_id,
                ServiceEntitlement.status == ServiceEntitlementStatus.active,
            )
        )
        if (
            existing_adjustment.preview_fingerprint != preview.fingerprint
            or existing_adjustment.account_id != preview.account_id
            or entitlement is None
            or entitlement.subscription_id != preview.subscription_id
            or _utc(entitlement.starts_at) != preview.starts_at
            or _utc(entitlement.ends_at) != preview.ends_at
            or round_money(entitlement.amount_funded) != preview.amount
            or entitlement.currency != preview.currency
        ):
            _error(
                "idempotency_conflict",
                "Prepaid renewal idempotency evidence does not match the request.",
            )
        return PrepaidServiceRenewalResult(
            preview=preview,
            adjustment=existing_adjustment,
            ledger_entry=existing_adjustment.ledger_entry,
            entitlement=entitlement,
            replayed=True,
        )

    subscription = _subscription_for_request(db, preview.subscription_id)
    db.refresh(subscription)
    current = preview_prepaid_service_renewal(
        db,
        subscription_id=subscription.id,
        starts_at=preview.starts_at,
        ends_at=preview.ends_at,
        amount=preview.amount,
        currency=preview.currency,
    )
    if current.fingerprint != preview.fingerprint:
        _error(
            "stale_preview",
            "Prepaid funding changed after preview; preview again.",
        )
    if not current.allowed:
        _error(
            "insufficient_funding",
            "Insufficient prepaid funding for service renewal.",
        )

    try:
        adjustment_result = stage_system_account_adjustment(
            db,
            StageSystemAccountAdjustmentCommand(
                context=CommandContext.system(
                    actor="system:prepaid_service_renewals",
                    scope=ACCOUNT_ADJUSTMENT_SCOPE,
                    reason="Stage one funded prepaid service-period debit",
                    idempotency_key=current.idempotency_key,
                ),
                request=AccountAdjustmentPreviewRequest(
                    account_id=current.account_id,
                    category=LedgerCategory.internet_service,
                    amount=current.amount,
                    currency=current.currency,
                    memo=(
                        "Prepaid service renewal "
                        f"{current.starts_at.date()} - {current.ends_at.date()}"
                    ),
                    reason="Funded prepaid service period",
                ),
                origin=_ORIGIN,
                origin_ref=current.origin_ref,
                idempotency_key=current.idempotency_key,
                ledger_effective_date=current.starts_at,
            ),
        )
    except AccountAdjustmentError as exc:
        _adjustment_error(exc)
    entitlement = ensure_prepaid_entitlement_for_wallet_debit(
        db,
        subscription=subscription,
        ledger_entry=adjustment_result.ledger_entry,
        starts_at=current.starts_at,
        ends_at=current.ends_at,
    )
    if entitlement is None:
        _error(
            "incomplete_entitlement",
            "Prepaid renewal did not produce exact entitlement evidence.",
        )
    metadata = dict(entitlement.metadata_ or {})
    metadata.update(
        {
            "evidence_ref": evidence,
            "preview_fingerprint": current.fingerprint,
            "idempotency_key": current.idempotency_key,
        }
    )
    entitlement.metadata_ = metadata
    if (
        subscription.next_billing_at is None
        or _utc(subscription.next_billing_at) < current.ends_at
    ):
        subscription.next_billing_at = current.ends_at
    db.flush()
    return PrepaidServiceRenewalResult(
        preview=current,
        adjustment=adjustment_result.adjustment,
        ledger_entry=adjustment_result.ledger_entry,
        entitlement=entitlement,
        replayed=adjustment_result.replayed,
    )


def stage_prepaid_service_renewed_outcome(
    db: Session,
    *,
    account_id: UUID,
    subscription_id: UUID,
    entitlement_id: UUID,
    ledger_entry_id: UUID,
    period_start: datetime,
    renewed_through: datetime,
    amount: Decimal,
    currency: str,
    source: PrepaidServiceRenewalSource,
    trigger_payment_id: UUID | None = None,
) -> PrepaidServiceRenewedOutcome:
    """Stage the exact forward-renewal outcome beside its financial writes."""
    from app.services.events.dispatcher import emit_event
    from app.services.events.types import EventType

    starts_at = _utc(period_start)
    ends_at = _utc(renewed_through)
    charge = round_money(amount)
    event = emit_event(
        db,
        EventType.prepaid_service_renewed,
        {
            "schema_version": 1,
            "subscription_id": str(subscription_id),
            "entitlement_id": str(entitlement_id),
            "ledger_entry_id": str(ledger_entry_id),
            "trigger_payment_id": (
                str(trigger_payment_id) if trigger_payment_id else None
            ),
            "amount": str(charge),
            "currency": currency,
            "period_start": starts_at.isoformat(),
            "renewed_through": ends_at.isoformat(),
            "source": source.value,
        },
        actor="system:prepaid_service_renewals",
        account_id=account_id,
        subscription_id=subscription_id,
    )
    return PrepaidServiceRenewedOutcome(
        event_id=event.event_id,
        account_id=account_id,
        subscription_id=subscription_id,
        entitlement_id=entitlement_id,
        ledger_entry_id=ledger_entry_id,
        period_start=starts_at,
        renewed_through=ends_at,
        amount=charge,
        currency=currency,
        source=source,
        trigger_payment_id=trigger_payment_id,
    )


def renewal_outcomes_for_payment(
    db: Session,
    payment_id: UUID,
) -> tuple[PrepaidServiceRenewedOutcome, ...]:
    """Return canonical renewal outcomes explicitly linked to one payment."""
    from app.models.event_store import EventStore
    from app.services.events.types import EventType

    rows = list(
        db.scalars(
            select(EventStore)
            .where(
                EventStore.event_type == EventType.prepaid_service_renewed.value,
                EventStore.is_active.is_(True),
                EventStore.payload["trigger_payment_id"].as_string() == str(payment_id),
            )
            .order_by(EventStore.created_at, EventStore.id)
        ).all()
    )
    outcomes: list[PrepaidServiceRenewedOutcome] = []
    for row in rows:
        payload = row.payload or {}
        if row.account_id is None or row.subscription_id is None:
            continue
        try:
            outcomes.append(
                PrepaidServiceRenewedOutcome(
                    event_id=row.event_id,
                    account_id=row.account_id,
                    subscription_id=row.subscription_id,
                    entitlement_id=UUID(str(payload["entitlement_id"])),
                    ledger_entry_id=UUID(str(payload["ledger_entry_id"])),
                    period_start=_utc(datetime.fromisoformat(payload["period_start"])),
                    renewed_through=_utc(
                        datetime.fromisoformat(payload["renewed_through"])
                    ),
                    amount=round_money(Decimal(str(payload["amount"]))),
                    currency=str(payload["currency"]),
                    source=PrepaidServiceRenewalSource(str(payload["source"])),
                    trigger_payment_id=payment_id,
                )
            )
        except (KeyError, TypeError, ValueError):
            # Malformed historical events are not a basis for a customer claim.
            continue
    return tuple(outcomes)


def _payable_invoice_exists(
    db: Session,
    *,
    account_id: UUID,
    currency: str,
) -> bool:
    return (
        db.scalar(
            select(Invoice.id)
            .where(
                Invoice.account_id == account_id,
                Invoice.is_active.is_(True),
                Invoice.status.in_(
                    {
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    }
                ),
                Invoice.currency == currency,
                Invoice.balance_due > Decimal("0.00"),
            )
            .limit(1)
        )
        is not None
    )


def apply_due_prepaid_service_after_funding_change(
    db: Session,
    *,
    account_id: UUID,
    effective_at: datetime,
    funding_currency: str,
    evidence_ref: str,
    trigger_payment_id: UUID | None = None,
) -> FundingChangeRenewalResult:
    """Consume newly available funding for currently due prepaid service.

    Payment settlement, account-credit settlement and invoice allocation remain
    separate owners. Their completed funding-change event invokes this owner
    only after ordinary payable invoices have had first claim on the credit. A
    lapsed service starts a new period on the payment day; missed inactive
    periods are never back-billed.
    """
    evaluated_at = _utc(effective_at)
    currency = str(funding_currency or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("funding_currency must be a three-letter code")
    evidence = evidence_ref.strip()
    if not evidence:
        raise ValueError("evidence_ref is required")

    due_subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.subscriber_id == account_id,
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES),
                Subscription.next_billing_at.isnot(None),
                Subscription.next_billing_at <= evaluated_at,
                CatalogOffer.billing_cycle == BillingCycle.monthly,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(Subscription.next_billing_at, Subscription.id)
        ).all()
    )
    if not due_subscriptions:
        return FundingChangeRenewalResult(
            account_id=account_id,
            scanned=0,
            funded=0,
            unfunded=0,
            already_covered=0,
            missing_price=0,
            currency_mismatch=0,
            disposition=FundingChangeRenewalDisposition.no_due_service,
        )

    if _payable_invoice_exists(db, account_id=account_id, currency=currency):
        return FundingChangeRenewalResult(
            account_id=account_id,
            scanned=len(due_subscriptions),
            funded=0,
            unfunded=0,
            already_covered=0,
            missing_price=0,
            currency_mismatch=0,
            disposition=FundingChangeRenewalDisposition.payable_invoice_remaining,
        )

    from app.services.billing_automation import _period_end

    funded = 0
    unfunded = 0
    already_covered = 0
    missing_price = 0
    currency_mismatch = 0
    renewals: list[PrepaidServiceRenewedOutcome] = []
    paid_day = evaluated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    for subscription in due_subscriptions:
        charge = resolve_prepaid_monthly_charge(db, subscription, evaluated_at)
        if charge is None:
            missing_price += 1
            continue
        amount, charge_currency, cycle = charge
        if charge_currency != currency:
            currency_mismatch += 1
            continue
        anchor = _utc(subscription.next_billing_at or paid_day)
        period_start = max(anchor, paid_day)
        period_end = _period_end(period_start, cycle)
        paid_through = prepaid_entitlement_coverage_end(
            db,
            subscription_id=subscription.id,
            account_id=account_id,
            period_start=period_start,
            period_end=period_end,
        )
        if paid_through is not None and _utc(paid_through) > period_start:
            if anchor < _utc(paid_through):
                subscription.next_billing_at = paid_through
            already_covered += 1
            continue
        preview = preview_prepaid_service_renewal(
            db,
            subscription_id=subscription.id,
            starts_at=period_start,
            ends_at=period_end,
            amount=amount,
            currency=charge_currency,
        )
        if not preview.allowed:
            unfunded += 1
            continue
        renewal = confirm_prepaid_service_renewal(
            db,
            preview,
            evidence_ref=evidence,
        )
        if not renewal.replayed:
            renewals.append(
                stage_prepaid_service_renewed_outcome(
                    db,
                    account_id=renewal.preview.account_id,
                    subscription_id=renewal.preview.subscription_id,
                    entitlement_id=renewal.entitlement.id,
                    ledger_entry_id=renewal.ledger_entry.id,
                    period_start=renewal.preview.starts_at,
                    renewed_through=renewal.preview.ends_at,
                    amount=renewal.preview.amount,
                    currency=renewal.preview.currency,
                    source=PrepaidServiceRenewalSource.account_credit,
                    trigger_payment_id=trigger_payment_id,
                )
            )
        funded += 1

    db.flush()
    disposition = (
        FundingChangeRenewalDisposition.funded
        if funded
        else FundingChangeRenewalDisposition.already_covered
        if already_covered
        else FundingChangeRenewalDisposition.unfunded
        if unfunded
        else FundingChangeRenewalDisposition.missing_price
        if missing_price
        else FundingChangeRenewalDisposition.currency_mismatch
    )
    return FundingChangeRenewalResult(
        account_id=account_id,
        scanned=len(due_subscriptions),
        funded=funded,
        unfunded=unfunded,
        already_covered=already_covered,
        missing_price=missing_price,
        currency_mismatch=currency_mismatch,
        disposition=disposition,
        renewals=tuple(renewals),
    )


def run_due_prepaid_service_renewals(
    db: Session,
    *,
    run_at: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int | str]:
    """Fund currently due monthly periods without historical catch-up.

    The daily billing runner calls this only behind the canonical control. A
    stale anchor older than two days is reported for reviewed reconciliation,
    never silently back-billed. Global missing authority fails closed for the
    pass. Accounts excluded from the materialized authority cohort and an
    unexpected account-level missing baseline are reported and skipped so one
    unavailable position cannot block unrelated verified renewals.
    """
    from app.services.billing_automation import _period_end
    from app.services.prepaid_funding_reconstruction import (
        PrepaidFundingBaselineMissingError,
        authority_cutover_batch,
        prepaid_funding_quarantined_account_ids,
    )

    effective_at = _utc(run_at or datetime.now(UTC))
    authority = authority_cutover_batch(db)
    if authority is None:
        return {
            "prepaid_renewals_scanned": 0,
            "prepaid_renewals_funded": 0,
            "prepaid_renewals_unfunded": 0,
            "prepaid_renewals_already_covered": 0,
            "prepaid_renewals_stale_anchor": 0,
            "prepaid_renewals_missing_price": 0,
            "prepaid_renewals_quarantined": 0,
            "prepaid_renewals_missing_baseline": 0,
            "prepaid_renewals_restored": 0,
            "prepaid_renewals_skipped": "authority_not_materialized",
        }

    subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES),
                Subscription.next_billing_at.isnot(None),
                Subscription.next_billing_at <= effective_at,
                CatalogOffer.billing_cycle == BillingCycle.monthly,
                CatalogOffer.is_active.is_(True),
            )
            .order_by(Subscription.next_billing_at, Subscription.id)
        ).all()
    )
    summary: dict[str, int | str] = {
        "prepaid_renewals_scanned": len(subscriptions),
        "prepaid_renewals_funded": 0,
        "prepaid_renewals_unfunded": 0,
        "prepaid_renewals_already_covered": 0,
        "prepaid_renewals_stale_anchor": 0,
        "prepaid_renewals_missing_price": 0,
        "prepaid_renewals_quarantined": 0,
        "prepaid_renewals_missing_baseline": 0,
        "prepaid_renewals_restored": 0,
    }
    quarantined_account_ids = prepaid_funding_quarantined_account_ids(
        db,
        {subscription.subscriber_id for subscription in subscriptions},
    )
    authority_at = _utc(authority.position_at)
    for subscription in subscriptions:
        if subscription.subscriber_id in quarantined_account_ids:
            summary["prepaid_renewals_quarantined"] = (
                int(summary["prepaid_renewals_quarantined"]) + 1
            )
            continue
        next_billing_at = subscription.next_billing_at
        if next_billing_at is None:
            continue
        period_start = _utc(next_billing_at)
        lag = effective_at - period_start
        if period_start <= authority_at or lag > _MAX_AUTOMATIC_LAG:
            summary["prepaid_renewals_stale_anchor"] = (
                int(summary["prepaid_renewals_stale_anchor"]) + 1
            )
            continue
        charge = resolve_prepaid_monthly_charge(db, subscription, effective_at)
        if charge is None:
            summary["prepaid_renewals_missing_price"] = (
                int(summary["prepaid_renewals_missing_price"]) + 1
            )
            continue
        amount, currency, cycle = charge
        period_end = _period_end(period_start, cycle)
        paid_through = prepaid_entitlement_coverage_end(
            db,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
            period_start=period_start,
            period_end=period_end,
        )
        if paid_through is not None and _utc(paid_through) > period_start:
            if not dry_run and period_start < _utc(paid_through):
                subscription.next_billing_at = paid_through
            summary["prepaid_renewals_already_covered"] = (
                int(summary["prepaid_renewals_already_covered"]) + 1
            )
            continue
        try:
            preview = preview_prepaid_service_renewal(
                db,
                subscription_id=subscription.id,
                starts_at=period_start,
                ends_at=period_end,
                amount=amount,
                currency=currency,
            )
        except PrepaidFundingBaselineMissingError:
            # A baseline may become unavailable after the quarantine snapshot
            # above. Preview is read-only, so isolating this account cannot
            # retain a partial renewal write.
            summary["prepaid_renewals_missing_baseline"] = (
                int(summary["prepaid_renewals_missing_baseline"]) + 1
            )
            continue
        if not preview.allowed:
            summary["prepaid_renewals_unfunded"] = (
                int(summary["prepaid_renewals_unfunded"]) + 1
            )
            continue
        if not dry_run:
            renewal = confirm_prepaid_service_renewal(
                db,
                preview,
                evidence_ref=(
                    "scheduled-billing-run:"
                    f"{effective_at.isoformat().replace('+00:00', 'Z')}"
                ),
            )
            if not renewal.replayed:
                stage_prepaid_service_renewed_outcome(
                    db,
                    account_id=renewal.preview.account_id,
                    subscription_id=renewal.preview.subscription_id,
                    entitlement_id=renewal.entitlement.id,
                    ledger_entry_id=renewal.ledger_entry.id,
                    period_start=renewal.preview.starts_at,
                    renewed_through=renewal.preview.ends_at,
                    amount=renewal.preview.amount,
                    currency=renewal.preview.currency,
                    source=PrepaidServiceRenewalSource.scheduled,
                )
            from app.models.collections import FinancialAccessOrigin
            from app.services.collections._core import restore_account_services

            restored = restore_account_services(
                db,
                str(subscription.subscriber_id),
                origin=FinancialAccessOrigin.prepaid_enforcement,
                resolved_by=(
                    "prepaid_service_renewal:"
                    f"{subscription.id}:{period_start.isoformat()}"
                ),
            )
            summary["prepaid_renewals_restored"] = (
                int(summary["prepaid_renewals_restored"]) + restored
            )
        summary["prepaid_renewals_funded"] = int(summary["prepaid_renewals_funded"]) + 1
    db.flush()
    return summary


__all__ = [
    "FundingChangeRenewalDisposition",
    "FundingChangeRenewalResult",
    "PREPAID_SERVICE_RENEWAL_ELIGIBLE_STATUSES",
    "PrepaidServiceRenewalPreview",
    "PrepaidServiceRenewalError",
    "PrepaidServiceRenewalResult",
    "PrepaidServiceRenewalSource",
    "PrepaidServiceRenewedOutcome",
    "apply_due_prepaid_service_after_funding_change",
    "confirm_prepaid_service_renewal",
    "preview_prepaid_service_renewal",
    "renewal_outcomes_for_payment",
    "resolve_prepaid_monthly_charge",
    "run_due_prepaid_service_renewals",
    "stage_prepaid_service_renewed_outcome",
]
