"""Owner for funding one due prepaid service period from customer position.

Payment settlement remains the owner when a new top-up immediately renews a
service. This owner covers the other legitimate case: funding already exists in
the reviewed opening position or earlier native facts when the next service
cycle becomes due. It posts one preview-bound account adjustment, links one
active service entitlement to that exact debit, and advances the subscription
anchor in the caller's transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
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
from app.services.owner_commands import CommandContext
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    prepaid_entitlement_coverage_end,
)

_ORIGIN = AccountAdjustmentOrigin.prepaid_service_renewal
_ELIGIBLE_STATUSES = {
    SubscriptionStatus.active,
    SubscriptionStatus.blocked,
    SubscriptionStatus.suspended,
}
_MAX_AUTOMATIC_LAG = timedelta(days=2)


def _adjustment_http_error(exc: AccountAdjustmentError) -> HTTPException:
    status_code = {
        "financial.account_adjustments.account_not_found": 404,
        "financial.account_adjustments.invalid_configuration": 503,
        "financial.account_adjustments.insufficient_funding": 402,
        "financial.account_adjustments.stale_preview": 409,
        "financial.account_adjustments.idempotency_conflict": 409,
        "financial.account_adjustments.incomplete_evidence": 409,
        "financial.account_adjustments.write_conflict": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=status_code, detail=exc.message)


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


def _subscription_for_request(
    db: Session,
    subscription_id: object,
) -> Subscription:
    subscription = db.get(Subscription, coerce_uuid(subscription_id))
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if subscription.billing_mode != BillingMode.prepaid:
        raise HTTPException(
            status_code=409,
            detail="Only a prepaid subscription can receive a wallet-funded cycle",
        )
    if subscription.status not in _ELIGIBLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Subscription is not eligible for prepaid service renewal",
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
        raise HTTPException(status_code=400, detail="Renewal period must be positive")
    charge = round_money(amount)
    if charge <= Decimal("0.00"):
        raise HTTPException(status_code=400, detail="Renewal amount must be positive")
    unit = str(currency).strip().upper()
    if len(unit) != 3:
        raise HTTPException(status_code=400, detail="Renewal currency is invalid")

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
        raise HTTPException(
            status_code=409,
            detail="Prepaid service period already has active funding evidence",
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
        raise _adjustment_http_error(exc) from exc
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
    commit: bool = False,
) -> PrepaidServiceRenewalResult:
    """Lock, re-preview, and atomically stage debit + entitlement + anchor."""
    evidence = evidence_ref.strip()
    if not evidence:
        raise HTTPException(status_code=400, detail="evidence_ref is required")

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
            raise HTTPException(
                status_code=409,
                detail="Prepaid renewal idempotency evidence does not match request",
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
        raise HTTPException(
            status_code=409,
            detail="Prepaid funding changed after preview; preview again",
        )
    if not current.allowed:
        raise HTTPException(
            status_code=402,
            detail="Insufficient prepaid funding for service renewal",
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
        raise _adjustment_http_error(exc) from exc
    entitlement = ensure_prepaid_entitlement_for_wallet_debit(
        db,
        subscription=subscription,
        ledger_entry=adjustment_result.ledger_entry,
        starts_at=current.starts_at,
        ends_at=current.ends_at,
    )
    if entitlement is None:
        raise RuntimeError("Prepaid renewal owner produced no entitlement")
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
    if commit:
        db.commit()
        db.refresh(adjustment_result.adjustment)
        db.refresh(adjustment_result.ledger_entry)
        db.refresh(entitlement)
    return PrepaidServiceRenewalResult(
        preview=current,
        adjustment=adjustment_result.adjustment,
        ledger_entry=adjustment_result.ledger_entry,
        entitlement=entitlement,
        replayed=adjustment_result.replayed,
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
    never silently back-billed. Missing authority fails closed through the
    canonical funding resolver used by the preview.
    """
    from app.services.billing_automation import _period_end
    from app.services.prepaid_funding_reconstruction import authority_cutover_batch

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
            "prepaid_renewals_restored": 0,
            "prepaid_renewals_skipped": "authority_not_materialized",
        }

    subscriptions = list(
        db.scalars(
            select(Subscription)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(
                Subscription.billing_mode == BillingMode.prepaid,
                Subscription.status.in_(_ELIGIBLE_STATUSES),
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
        "prepaid_renewals_restored": 0,
    }
    authority_at = _utc(authority.position_at)
    for subscription in subscriptions:
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
        preview = preview_prepaid_service_renewal(
            db,
            subscription_id=subscription.id,
            starts_at=period_start,
            ends_at=period_end,
            amount=amount,
            currency=currency,
        )
        if not preview.allowed:
            summary["prepaid_renewals_unfunded"] = (
                int(summary["prepaid_renewals_unfunded"]) + 1
            )
            continue
        if not dry_run:
            confirm_prepaid_service_renewal(
                db,
                preview,
                evidence_ref=(
                    "scheduled-billing-run:"
                    f"{effective_at.isoformat().replace('+00:00', 'Z')}"
                ),
                commit=False,
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
    "PrepaidServiceRenewalPreview",
    "PrepaidServiceRenewalResult",
    "confirm_prepaid_service_renewal",
    "preview_prepaid_service_renewal",
    "resolve_prepaid_monthly_charge",
    "run_due_prepaid_service_renewals",
]
