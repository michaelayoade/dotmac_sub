"""Canonical prepaid plan-change pricing and financial adjustment workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    AccountAdjustment,
    CreditNote,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
)
from app.models.catalog import (
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferVersionPrice,
    PriceType,
    Subscription,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.billing import (
    AccountAdjustmentPreviewRequest,
    CreditNoteIssuePreviewRequest,
)
from app.services.common import coerce_uuid, round_money
from app.services.customer_financial_position import get_customer_financial_position

_DEBIT_SCOPE = "prepaid_plan_change_debit"
_CREDIT_SCOPE = "prepaid_plan_change_credit"


@dataclass(frozen=True)
class PrepaidPlanChangeDecision:
    """One financial answer shared by previews and committed plan changes."""

    account_id: str
    subscription_id: str
    current_offer_id: str
    target_offer_id: str
    subscription_status: str
    billing_mode: str
    currency: str
    proration: dict[str, Any]
    prepaid_funding_before: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    required_amount: Decimal
    shortfall: Decimal
    allowed: bool
    reason: str | None

    @property
    def is_prepaid(self) -> bool:
        return self.billing_mode == BillingMode.prepaid.value

    @property
    def net_amount(self) -> Decimal:
        return round_money(Decimal(str(self.proration.get("net_amount", "0.00"))))

    @property
    def prepaid_funding_after(self) -> Decimal:
        if not self.has_financial_effect:
            return self.prepaid_funding_before
        return round_money(self.prepaid_funding_before - self.net_amount)

    @property
    def has_financial_effect(self) -> bool:
        return (
            self.is_prepaid
            and self.subscription_status == "active"
            and bool(self.proration.get("generate_now", False))
            and self.net_amount != Decimal("0.00")
        )

    @property
    def ledger_entry_type(self) -> LedgerEntryType | None:
        if not self.has_financial_effect:
            return None
        return (
            LedgerEntryType.debit
            if self.net_amount > Decimal("0.00")
            else LedgerEntryType.credit
        )

    @property
    def ledger_source(self) -> LedgerSource | None:
        if not self.has_financial_effect:
            return None
        return (
            LedgerSource.adjustment
            if self.net_amount > Decimal("0.00")
            else LedgerSource.credit_note
        )

    @property
    def ledger_amount(self) -> Decimal:
        return abs(self.net_amount) if self.has_financial_effect else Decimal("0.00")

    @property
    def access_consequence(self) -> str:
        return "none_plan_change_only"

    @property
    def fingerprint(self) -> str:
        """Fingerprint the exact human-visible financial decision.

        Exact cycle seconds are deliberately excluded: they continue to tick
        while a person reads the confirmation. The displayed, rounded monetary
        result and policy decision remain bound; crossing a pricing-cent,
        funding, receivable, eligibility, or catalog boundary makes it stale.
        """
        values = {
            "account_id": self.account_id,
            "subscription_id": self.subscription_id,
            "current_offer_id": self.current_offer_id,
            "target_offer_id": self.target_offer_id,
            "subscription_status": self.subscription_status,
            "billing_mode": self.billing_mode,
            "currency": self.currency,
            "credit_amount": _money(self.proration.get("credit_amount")),
            "charge_amount": _money(self.proration.get("charge_amount")),
            "net_amount": _money(self.net_amount),
            "fee_amount": _money(self.proration.get("fee_amount")),
            "generate_now": bool(self.proration.get("generate_now", False)),
            "invoice_timing": str(self.proration.get("invoice_timing", "")),
            "minimum_invoice_amount": _money(
                self.proration.get("minimum_invoice_amount")
            ),
            "days_remaining": int(self.proration.get("days_remaining", 0) or 0),
            "days_in_cycle": int(self.proration.get("days_in_cycle", 0) or 0),
            "prepaid_funding_before": _money(self.prepaid_funding_before),
            "prepaid_funding_after": _money(self.prepaid_funding_after),
            "postpaid_receivables": _money(self.postpaid_receivables),
            "collection_blocking_balance": _money(self.collection_blocking_balance),
            "required_amount": _money(self.required_amount),
            "shortfall": _money(self.shortfall),
            "allowed": self.allowed,
            "reason": self.reason,
            "ledger_entry_type": (
                self.ledger_entry_type.value if self.ledger_entry_type else None
            ),
            "ledger_source": self.ledger_source.value if self.ledger_source else None,
            "ledger_amount": _money(self.ledger_amount),
            "access_consequence": self.access_consequence,
        }
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_evidence_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "subscription_id": self.subscription_id,
            "current_offer_id": self.current_offer_id,
            "target_offer_id": self.target_offer_id,
            "subscription_status": self.subscription_status,
            "billing_mode": self.billing_mode,
            "currency": self.currency,
            "prepaid_funding_before": _money(self.prepaid_funding_before),
            "prepaid_funding_after": _money(self.prepaid_funding_after),
            "postpaid_receivables": _money(self.postpaid_receivables),
            "collection_blocking_balance": _money(self.collection_blocking_balance),
            "required_amount": _money(self.required_amount),
            "shortfall": _money(self.shortfall),
            "credit_amount": _money(self.proration.get("credit_amount")),
            "charge_amount": _money(self.proration.get("charge_amount")),
            "net_amount": _money(self.net_amount),
            "allowed": self.allowed,
            "reason": self.reason,
            "ledger_entry_type": (
                self.ledger_entry_type.value if self.ledger_entry_type else None
            ),
            "ledger_source": self.ledger_source.value if self.ledger_source else None,
            "ledger_amount": _money(self.ledger_amount),
            "access_consequence": self.access_consequence,
            "preview_fingerprint": self.fingerprint,
        }

    def as_quote_dict(self) -> dict[str, object]:
        return {
            "current_remaining_value": round_money(
                Decimal(str(self.proration.get("credit_amount", "0.00")))
            ),
            "required_amount": self.required_amount,
            "prepaid_funding_before": self.prepaid_funding_before,
            "prepaid_funding_after": self.prepaid_funding_after,
            "postpaid_receivables": self.postpaid_receivables,
            "currency": self.currency,
            "shortfall": self.shortfall,
            "collection_blocking_balance": self.collection_blocking_balance,
            "charge_amount": round_money(
                Decimal(str(self.proration.get("charge_amount", "0.00")))
            ),
            "net_amount": self.net_amount,
            "days_remaining": int(self.proration.get("days_remaining", 0) or 0),
            "days_in_cycle": int(self.proration.get("days_in_cycle", 0) or 0),
            "remaining_cycle_seconds": int(
                self.proration.get("remaining_cycle_seconds", 0) or 0
            ),
            "total_cycle_seconds": int(
                self.proration.get("total_cycle_seconds", 0) or 0
            ),
            "can_apply_immediately": self.allowed,
            "is_upgrade": self.required_amount > Decimal("0.00"),
            "is_downgrade": self.net_amount < Decimal("0.00"),
            "reason": self.reason,
            "preview_fingerprint": self.fingerprint,
            "ledger_entry_type": (
                self.ledger_entry_type.value if self.ledger_entry_type else None
            ),
            "ledger_source": self.ledger_source.value if self.ledger_source else None,
            "ledger_amount": self.ledger_amount,
            "access_consequence": self.access_consequence,
        }

    def rejection_detail(self) -> dict[str, str]:
        if self.reason == "catalog_currency_mismatch":
            return {
                "code": "catalog_currency_mismatch",
                "message": (
                    "The current and target plans use different currencies. "
                    "Create a reviewed migration instead of applying an immediate change."
                ),
            }
        if self.reason == "collection_blocking_balance":
            return {
                "code": "collection_blocking_balance",
                "message": (
                    "The customer has overdue debt. Settle it or schedule the plan "
                    "change for the next cycle."
                ),
                "collection_blocking_balance": str(self.collection_blocking_balance),
            }
        return {
            "code": "insufficient_prepaid_funding",
            "message": (
                "Insufficient prepaid balance for this plan change. Top up the "
                "customer's prepaid funding or schedule the change for the next cycle."
            ),
            "required_amount": str(self.required_amount),
            "prepaid_funding_before": str(self.prepaid_funding_before),
            "prepaid_funding_after": str(self.prepaid_funding_after),
            "postpaid_receivables": str(self.postpaid_receivables),
            "shortfall": str(self.shortfall),
        }


@dataclass(frozen=True)
class PreparedPrepaidPlanChange:
    decision: PrepaidPlanChangeDecision
    account_adjustment: AccountAdjustment | None = None
    ledger_entry: LedgerEntry | None = None
    credit_note: CreditNote | None = None
    replayed: bool = False


def _money(value: object) -> str:
    return f"{round_money(Decimal(str(value or '0.00'))):.2f}"


def _require_confirmed_preview(
    decision: PrepaidPlanChangeDecision,
    expected_preview_fingerprint: str | None,
) -> None:
    expected = (expected_preview_fingerprint or "").strip()
    if not expected:
        if decision.is_prepaid and decision.subscription_status == "active":
            raise PrepaidPlanChangePreviewRequired(decision)
        return
    if expected != decision.fingerprint:
        raise PrepaidPlanChangePreviewStale(decision)


def resolve_prepaid_plan_change(
    db: Session,
    subscription: Subscription,
    target_offer_id: str,
    *,
    effective_at: datetime | None = None,
    prepaid_funding_before: Decimal | None = None,
) -> PrepaidPlanChangeDecision:
    """Resolve pricing, debt policy, funding, and the final decision."""
    from app.services.catalog.subscriptions import (
        _apply_plan_change_policy,
        _calculate_proration,
    )

    proration = _calculate_proration(
        db,
        subscription,
        target_offer_id,
        effective_at=effective_at,
    )
    proration = _apply_plan_change_policy(
        db,
        proration,
        old_price=Decimal(str(proration.get("old_price", "0.00"))),
        new_price=Decimal(str(proration.get("new_price", "0.00"))),
    )
    account_id = str(subscription.subscriber_id)
    position = get_customer_financial_position(db, account_id)
    available = round_money(
        position.prepaid_available_balance
        if prepaid_funding_before is None
        else prepaid_funding_before
    )
    blocking_balance = round_money(position.collection_blocking_balance)
    billing_mode = str(
        getattr(subscription.billing_mode, "value", subscription.billing_mode or "")
    )
    subscription_status = str(
        getattr(subscription.status, "value", subscription.status or "")
    )
    net_amount = round_money(Decimal(str(proration.get("net_amount", "0.00"))))
    old_currency = _subscription_price_currency(db, subscription)
    target_currency = _offer_price_currency(db, target_offer_id)
    generate_now = bool(proration.get("generate_now", False))
    required_amount = (
        round_money(max(Decimal("0.00"), net_amount))
        if (
            billing_mode == BillingMode.prepaid.value
            and subscription_status == "active"
            and generate_now
        )
        else Decimal("0.00")
    )
    shortfall = round_money(max(Decimal("0.00"), required_amount - available))

    reason: str | None = None
    if billing_mode == BillingMode.prepaid.value and subscription_status == "active":
        if net_amount != Decimal("0.00") and old_currency != target_currency:
            reason = "catalog_currency_mismatch"
        elif blocking_balance > Decimal("0.00"):
            reason = "collection_blocking_balance"
        elif shortfall > Decimal("0.00"):
            reason = "insufficient_prepaid_funding"

    return PrepaidPlanChangeDecision(
        account_id=account_id,
        subscription_id=str(subscription.id),
        current_offer_id=str(subscription.offer_id),
        target_offer_id=str(target_offer_id),
        subscription_status=subscription_status,
        billing_mode=billing_mode,
        currency=target_currency,
        proration=proration,
        prepaid_funding_before=available,
        postpaid_receivables=round_money(position.open_invoice_balance),
        collection_blocking_balance=blocking_balance,
        required_amount=required_amount,
        shortfall=shortfall,
        allowed=reason is None,
        reason=reason,
    )


def _offer_price_currency(db: Session, offer_id: object) -> str:
    row = (
        db.query(OfferPrice.currency)
        .filter(OfferPrice.offer_id == offer_id)
        .filter(OfferPrice.price_type == PriceType.recurring)
        .filter(OfferPrice.is_active.is_(True))
        .first()
    )
    return str(row[0] or "NGN") if row else "NGN"


def _subscription_price_currency(db: Session, subscription: Subscription) -> str:
    if subscription.offer_version_id:
        row = (
            db.query(OfferVersionPrice.currency)
            .filter(OfferVersionPrice.offer_version_id == subscription.offer_version_id)
            .filter(OfferVersionPrice.price_type == PriceType.recurring)
            .filter(OfferVersionPrice.is_active.is_(True))
            .first()
        )
        if row:
            return str(row[0] or "NGN")
    return _offer_price_currency(db, subscription.offer_id)


def _stable_operation_key(
    subscription: Subscription,
    target_offer_id: str,
    operation_key: str | None,
) -> str:
    raw = (operation_key or "").strip()
    if not raw:
        raw = ":".join(
            (
                str(subscription.id),
                str(subscription.offer_id),
                str(target_offer_id),
                subscription.next_billing_at.isoformat()
                if subscription.next_billing_at
                else "no-billing-anchor",
            )
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_reservation(
    db: Session,
    *,
    scope: str,
    key: str,
) -> IdempotencyKey | None:
    return db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
    ).first()


def _existing_adjustment(
    db: Session,
    *,
    reservation: IdempotencyKey,
    account_id: str,
    model,
):
    if str(reservation.account_id) != account_id:
        raise ValueError("Plan-change idempotency key belongs to another account")
    if not reservation.ref_id:
        raise ValueError("Plan-change financial adjustment is already in progress")
    adjustment = db.get(model, coerce_uuid(reservation.ref_id))
    if adjustment is None:
        raise ValueError("Plan-change idempotency record has no financial adjustment")
    return adjustment


def prepare_immediate_prepaid_plan_change(
    db: Session,
    subscription: Subscription,
    target_offer: CatalogOffer,
    *,
    old_offer_name: str,
    operation_key: str | None = None,
    effective_at: datetime | None = None,
    expected_preview_fingerprint: str | None = None,
) -> PreparedPrepaidPlanChange:
    """Lock, recompute, validate, and stage the prepaid adjustment atomically.

    This function does not commit. The caller must mutate the subscription and
    commit both changes together.
    """
    from app.services.billing._common import lock_account
    from app.services.billing.credit_notes import CreditNotes

    account_id = str(subscription.subscriber_id)
    lock_account(db, account_id)
    db.refresh(subscription)
    decision = resolve_prepaid_plan_change(
        db,
        subscription,
        str(target_offer.id),
        effective_at=effective_at,
    )
    if not decision.is_prepaid:
        _require_confirmed_preview(decision, expected_preview_fingerprint)
        return PreparedPrepaidPlanChange(decision=decision)
    key = _stable_operation_key(subscription, str(target_offer.id), operation_key)
    adjustment_key = f"plan-adjustment-{hashlib.sha256(key.encode()).hexdigest()}"
    prior_debit = _find_reservation(db, scope=_DEBIT_SCOPE, key=key)
    if prior_debit is not None:
        entry = _existing_adjustment(
            db,
            reservation=prior_debit,
            account_id=account_id,
            model=LedgerEntry,
        )
        # The staged/committed debit is already included in the current ledger
        # projection. Add it back only for the replay decision so an exact-
        # balance operation remains replayable instead of appearing unfunded.
        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            str(target_offer.id),
            effective_at=effective_at,
            prepaid_funding_before=decision.prepaid_funding_before + entry.amount,
        )
        _require_confirmed_preview(decision, expected_preview_fingerprint)
        adjustment = db.scalar(
            select(AccountAdjustment).where(
                AccountAdjustment.ledger_entry_id == entry.id
            )
        )
        return PreparedPrepaidPlanChange(
            decision=decision,
            account_adjustment=adjustment,
            ledger_entry=entry,
            replayed=True,
        )
    prior_credit = _find_reservation(db, scope=_CREDIT_SCOPE, key=key)
    if prior_credit is not None:
        credit_note = _existing_adjustment(
            db,
            reservation=prior_credit,
            account_id=account_id,
            model=CreditNote,
        )
        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            str(target_offer.id),
            effective_at=effective_at,
            prepaid_funding_before=(
                decision.prepaid_funding_before - credit_note.total
            ),
        )
        _require_confirmed_preview(decision, expected_preview_fingerprint)
        return PreparedPrepaidPlanChange(
            decision=decision,
            credit_note=credit_note,
            ledger_entry=credit_note.funding_ledger_entry,
            replayed=True,
        )
    prior_adjustment = db.scalar(
        select(AccountAdjustment).where(
            AccountAdjustment.origin == "prepaid_plan_change",
            AccountAdjustment.idempotency_key == adjustment_key,
        )
    )
    if prior_adjustment is not None:
        if str(prior_adjustment.account_id) != account_id:
            raise ValueError("Plan-change adjustment belongs to another account")
        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            str(target_offer.id),
            effective_at=effective_at,
            prepaid_funding_before=(
                decision.prepaid_funding_before + prior_adjustment.amount
            ),
        )
        _require_confirmed_preview(decision, expected_preview_fingerprint)
        return PreparedPrepaidPlanChange(
            decision=decision,
            account_adjustment=prior_adjustment,
            ledger_entry=prior_adjustment.ledger_entry,
            replayed=True,
        )
    _require_confirmed_preview(decision, expected_preview_fingerprint)
    if not decision.allowed:
        raise PrepaidPlanChangeRejected(decision)

    net_amount = decision.net_amount
    if not decision.has_financial_effect:
        return PreparedPrepaidPlanChange(decision=decision)

    if net_amount > 0:
        from app.services.billing.adjustments import AccountAdjustments

        adjustment_result = AccountAdjustments.confirm_system(
            db,
            AccountAdjustmentPreviewRequest(
                account_id=subscription.subscriber_id,
                category=LedgerCategory.internet_service,
                amount=decision.ledger_amount,
                currency=decision.currency,
                memo=(
                    "Prepaid plan change charge: "
                    f"{old_offer_name} -> {target_offer.name}"
                ),
                reason="Immediate prepaid plan-change proration",
            ),
            origin="prepaid_plan_change",
            origin_ref=f"{subscription.id}:{target_offer.id}",
            idempotency_key=adjustment_key,
            commit=False,
        )
        return PreparedPrepaidPlanChange(
            decision=decision,
            account_adjustment=adjustment_result.adjustment,
            ledger_entry=adjustment_result.ledger_entry,
            replayed=adjustment_result.replayed,
        )

    prior = _find_reservation(db, scope=_CREDIT_SCOPE, key=key)
    if prior is not None:
        credit_note = _existing_adjustment(
            db,
            reservation=prior,
            account_id=account_id,
            model=CreditNote,
        )
        return PreparedPrepaidPlanChange(
            decision=decision,
            credit_note=credit_note,
            ledger_entry=credit_note.funding_ledger_entry,
            replayed=True,
        )
    credit_amount = abs(net_amount)
    credit_result = CreditNotes.issue_system(
        db,
        CreditNoteIssuePreviewRequest(
            account_id=subscription.subscriber_id,
            currency=decision.currency,
            subtotal=credit_amount,
            tax_total=Decimal("0.00"),
            total=credit_amount,
            memo=(
                f"Plan change credit: {old_offer_name} -> {target_offer.name} "
                f"({decision.proration.get('days_remaining', 0)} days remaining)"
            ),
            line_description=f"Plan change credit: {old_offer_name} -> {target_offer.name}",
        ),
        idempotency_key=f"plan-credit-{hashlib.sha256(key.encode()).hexdigest()}",
        commit=False,
    )
    credit_note = credit_result.credit_note
    db.add(
        IdempotencyKey(
            scope=_CREDIT_SCOPE,
            key=key,
            account_id=subscription.subscriber_id,
            ref_id=str(credit_note.id),
        )
    )
    db.flush()
    return PreparedPrepaidPlanChange(
        decision=decision,
        credit_note=credit_note,
        ledger_entry=credit_result.funding_ledger_entry,
        replayed=credit_result.idempotent_replay,
    )


class PrepaidPlanChangeRejected(ValueError):
    def __init__(self, decision: PrepaidPlanChangeDecision):
        super().__init__(decision.reason or "prepaid_plan_change_rejected")
        self.decision = decision


class PrepaidPlanChangePreviewRequired(ValueError):
    def __init__(self, decision: PrepaidPlanChangeDecision):
        super().__init__("Preview and confirm this financial plan change first")
        self.decision = decision


class PrepaidPlanChangePreviewStale(ValueError):
    def __init__(self, decision: PrepaidPlanChangeDecision):
        super().__init__("Financial state changed after preview; preview again")
        self.decision = decision


__all__ = [
    "PrepaidPlanChangeDecision",
    "PrepaidPlanChangeRejected",
    "PrepaidPlanChangePreviewRequired",
    "PrepaidPlanChangePreviewStale",
    "PreparedPrepaidPlanChange",
    "prepare_immediate_prepaid_plan_change",
    "resolve_prepaid_plan_change",
]
