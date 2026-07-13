"""Canonical prepaid plan-change pricing and financial adjustment workflow."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNote,
    CreditNoteStatus,
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
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.schemas.billing import LedgerEntryCreate
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
    billing_mode: str
    currency: str
    proration: dict[str, Any]
    current_balance: Decimal
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

    def as_quote_dict(self) -> dict[str, object]:
        return {
            "current_remaining_value": round_money(
                Decimal(str(self.proration.get("credit_amount", "0.00")))
            ),
            "required_amount": self.required_amount,
            "current_balance": self.current_balance,
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
            "code": "insufficient_prepaid_balance",
            "message": (
                "Insufficient prepaid balance for this plan change. Top up the "
                "customer wallet or schedule the change for the next cycle."
            ),
            "required_amount": str(self.required_amount),
            "current_balance": str(self.current_balance),
            "shortfall": str(self.shortfall),
        }


@dataclass(frozen=True)
class PreparedPrepaidPlanChange:
    decision: PrepaidPlanChangeDecision
    ledger_entry: LedgerEntry | None = None
    credit_note: CreditNote | None = None
    replayed: bool = False


def resolve_prepaid_plan_change(
    db: Session,
    subscription: Subscription,
    target_offer_id: str,
    *,
    effective_at: datetime | None = None,
    current_balance: Decimal | None = None,
) -> PrepaidPlanChangeDecision:
    """Resolve pricing, debt policy, wallet affordability, and the final decision."""
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
        if current_balance is None
        else current_balance
    )
    blocking_balance = round_money(position.collection_blocking_balance)
    billing_mode = str(
        getattr(subscription.billing_mode, "value", subscription.billing_mode or "")
    )
    net_amount = round_money(Decimal(str(proration.get("net_amount", "0.00"))))
    old_currency = _subscription_price_currency(db, subscription)
    target_currency = _offer_price_currency(db, target_offer_id)
    generate_now = bool(proration.get("generate_now", False))
    required_amount = (
        round_money(max(Decimal("0.00"), net_amount))
        if billing_mode == BillingMode.prepaid.value and generate_now
        else Decimal("0.00")
    )
    shortfall = round_money(max(Decimal("0.00"), required_amount - available))

    reason: str | None = None
    if billing_mode == BillingMode.prepaid.value:
        if net_amount != Decimal("0.00") and old_currency != target_currency:
            reason = "catalog_currency_mismatch"
        elif blocking_balance > Decimal("0.00"):
            reason = "collection_blocking_balance"
        elif shortfall > Decimal("0.00"):
            reason = "insufficient_prepaid_balance"

    return PrepaidPlanChangeDecision(
        account_id=account_id,
        subscription_id=str(subscription.id),
        current_offer_id=str(subscription.offer_id),
        target_offer_id=str(target_offer_id),
        billing_mode=billing_mode,
        currency=target_currency,
        proration=proration,
        current_balance=available,
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
) -> PreparedPrepaidPlanChange:
    """Lock, recompute, validate, and stage the prepaid adjustment atomically.

    This function does not commit. The caller must mutate the subscription and
    commit both changes together.
    """
    from app.services import numbering
    from app.services.billing._common import lock_account

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
        return PreparedPrepaidPlanChange(decision=decision)
    key = _stable_operation_key(subscription, str(target_offer.id), operation_key)
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
            current_balance=decision.current_balance + entry.amount,
        )
        return PreparedPrepaidPlanChange(
            decision=decision,
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
        return PreparedPrepaidPlanChange(
            decision=decision,
            credit_note=credit_note,
            replayed=True,
        )
    if not decision.allowed:
        raise PrepaidPlanChangeRejected(decision)

    net_amount = decision.net_amount
    if not bool(decision.proration.get("generate_now", False)) or net_amount == 0:
        return PreparedPrepaidPlanChange(decision=decision)

    if net_amount > 0:
        from app.services.billing.ledger import LedgerEntries

        prior = _find_reservation(db, scope=_DEBIT_SCOPE, key=key)
        if prior is not None:
            entry = _existing_adjustment(
                db,
                reservation=prior,
                account_id=account_id,
                model=LedgerEntry,
            )
            return PreparedPrepaidPlanChange(
                decision=decision,
                ledger_entry=entry,
                replayed=True,
            )
        entry = LedgerEntries.create(
            db,
            LedgerEntryCreate(
                account_id=subscription.subscriber_id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.adjustment,
                category=LedgerCategory.internet_service,
                amount=decision.required_amount,
                currency=decision.currency,
                memo=(
                    "Prepaid plan change charge: "
                    f"{old_offer_name} -> {target_offer.name}"
                ),
            ),
            commit=False,
        )
        db.add(
            IdempotencyKey(
                scope=_DEBIT_SCOPE,
                key=key,
                account_id=subscription.subscriber_id,
                ref_id=str(entry.id),
            )
        )
        db.flush()
        return PreparedPrepaidPlanChange(decision=decision, ledger_entry=entry)

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
            replayed=True,
        )
    credit_amount = abs(net_amount)
    credit_note = CreditNote(
        account_id=subscription.subscriber_id,
        credit_number=numbering.generate_number(
            db,
            SettingDomain.billing,
            "credit_note_number",
            "credit_note_number_enabled",
            "credit_note_number_prefix",
            "credit_note_number_padding",
            "credit_note_number_start",
        ),
        currency=decision.currency,
        subtotal=credit_amount,
        tax_total=Decimal("0.00"),
        total=credit_amount,
        status=CreditNoteStatus.issued,
        memo=(
            f"Plan change credit: {old_offer_name} -> {target_offer.name} "
            f"({decision.proration.get('days_remaining', 0)} days remaining)"
        ),
    )
    db.add(credit_note)
    db.flush()
    db.add(
        IdempotencyKey(
            scope=_CREDIT_SCOPE,
            key=key,
            account_id=subscription.subscriber_id,
            ref_id=str(credit_note.id),
        )
    )
    db.flush()
    return PreparedPrepaidPlanChange(decision=decision, credit_note=credit_note)


class PrepaidPlanChangeRejected(ValueError):
    def __init__(self, decision: PrepaidPlanChangeDecision):
        super().__init__(decision.reason or "prepaid_plan_change_rejected")
        self.decision = decision


__all__ = [
    "PrepaidPlanChangeDecision",
    "PrepaidPlanChangeRejected",
    "PreparedPrepaidPlanChange",
    "prepare_immediate_prepaid_plan_change",
    "resolve_prepaid_plan_change",
]
