"""Autopay engine — auto-charge a saved card on due invoices.

A customer opts in (one mandate per account, pointing at a saved card). The
Celery task calls :func:`run_all_due`, which for each active mandate charges the
saved card via Paystack ``charge_authorization`` for every open invoice and
records the payment through the existing billing adapter. All charges are
server-to-server; nothing here needs the customer present.
"""

from __future__ import annotations

import builtins
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.autopay import AutopayMandate
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentAllocationApply
from app.services import billing as billing_service
from app.services import paystack
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.common import coerce_uuid, round_money, to_decimal

logger = logging.getLogger(__name__)

_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def _mandate(db: Session, account_id: str) -> AutopayMandate | None:
    return db.scalars(
        select(AutopayMandate).where(
            AutopayMandate.account_id == coerce_uuid(account_id)
        )
    ).first()


def get_status(db: Session, account_id: str) -> dict:
    m = _mandate(db, account_id)
    return {
        "enabled": bool(m and m.is_active),
        "payment_method_id": str(m.payment_method_id)
        if m and m.payment_method_id
        else None,
    }


def _default_card(db: Session, account_id: str) -> PaymentMethod | None:
    cards = [
        c
        for c in billing_service.payment_methods.list(
            db, str(account_id), True, "created_at", "desc", 100, 0
        )
        if c.method_type == PaymentMethodType.card and c.token
    ]
    if not cards:
        return None
    return next((c for c in cards if c.is_default), cards[0])


def enable(
    db: Session, account_id: str, payment_method_id: str | None = None
) -> AutopayMandate:
    """Turn on autopay against a saved card (the default card if unspecified)."""
    if payment_method_id:
        card = billing_service.payment_methods.get(db, str(payment_method_id))
        if not card or str(card.account_id) != str(account_id) or not card.token:
            raise ValueError("Saved card not found")
    else:
        card = _default_card(db, account_id)
        if card is None:
            raise ValueError("Add a saved card before enabling autopay")

    m = _mandate(db, account_id)
    if m is None:
        m = AutopayMandate(
            account_id=coerce_uuid(str(account_id)),
            payment_method_id=card.id,
            is_active=True,
        )
        db.add(m)
    else:
        m.is_active = True
        m.payment_method_id = card.id
    db.commit()
    db.refresh(m)
    return m


def disable(db: Session, account_id: str) -> bool:
    m = _mandate(db, account_id)
    if m is None or not m.is_active:
        return False
    m.is_active = False
    db.commit()
    return True


def _open_invoices(db: Session, account_id: str) -> builtins.list[Invoice]:
    return list(
        db.scalars(
            select(Invoice).where(
                Invoice.account_id == coerce_uuid(account_id),
                Invoice.is_active.is_(True),
                Invoice.status.in_(_OPEN_STATUSES),
                Invoice.balance_due > 0,
            )
        ).all()
    )


def _email(db: Session, account_id: str) -> str:
    subscriber = db.get(Subscriber, coerce_uuid(account_id))
    return str(getattr(subscriber, "email", "") or "")


def _autopay_reference(invoice_id: str, amount_kobo: int) -> str:
    """Deterministic provider reference for one (invoice, amount) charge.

    Re-using the same reference makes the charge idempotent at Paystack — an
    overlapping run, or a retry after a record failure, resolves to the SAME
    transaction instead of capturing the card twice. The amount is in the key so
    a legitimately re-opened invoice (e.g. after a partial payment) gets a fresh
    reference.
    """
    return f"AUTOPAY-{invoice_id}-{amount_kobo}"


def _already_recorded(db: Session, reference: str) -> bool:
    return (
        db.scalars(select(Payment).where(Payment.external_id == reference)).first()
        is not None
    )


def _recover_charge(db: Session, reference: str) -> dict | None:
    """Best-effort: fetch an existing transaction by reference (used when a
    charge attempt errored but may already have captured). Returns the tx data
    or None if it can't be confirmed successful."""
    try:
        tx = paystack.verify_transaction(db, reference)
    except Exception:  # noqa: BLE001 - couldn't confirm; treat as not charged
        return None
    return tx if str(tx.get("status")) == "success" else None


def _lock_account(db: Session, account_id: str) -> None:
    """Serialize concurrent autopay runs for one account (Postgres only); SQLite
    serializes writes globally so this is a no-op there."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.query(Subscriber).filter(
            Subscriber.id == coerce_uuid(account_id)
        ).with_for_update().first()


def run_account_autopay(db: Session, account_id: str) -> dict:
    """Charge the account's saved card for each open invoice. Charges are
    idempotent on a deterministic per-(invoice, amount) reference, so overlapping
    runs and record-failure retries never capture the card twice."""
    mandate = _mandate(db, account_id)
    if mandate is None or not mandate.is_active:
        return {"charged": 0, "failed": 0, "skipped": "not_enabled"}

    # Serialize concurrent runs for this account so two runs can't both charge
    # the same invoice before either records.
    _lock_account(db, account_id)

    token = (
        billing_service.payment_methods.get_decrypted_token(
            db, str(mandate.payment_method_id)
        )
        if mandate.payment_method_id
        else None
    )
    if not token:
        return {"charged": 0, "failed": 0, "skipped": "no_saved_card"}

    email = _email(db, account_id)
    charged = 0
    failed = 0
    total = Decimal("0.00")

    for invoice in _open_invoices(db, account_id):
        amount = round_money(to_decimal(getattr(invoice, "balance_due", 0)))
        if amount <= Decimal("0.00"):
            continue
        amount_kobo = paystack.amount_to_kobo(amount)
        reference = _autopay_reference(str(invoice.id), amount_kobo)

        # Already charged + recorded this exact (invoice, amount)? Skip — never
        # capture the card twice for the same balance.
        if _already_recorded(db, reference):
            continue

        try:
            tx = paystack.charge_authorization(
                db,
                authorization_code=token,
                email=email,
                amount_kobo=amount_kobo,
                reference=reference,
                metadata={"invoice_id": str(invoice.id), "autopay": True},
            )
        except Exception:  # noqa: BLE001 - one bad charge must not abort the run
            # The charge may have actually gone through (e.g. a duplicate-reference
            # error after a prior capture). Recover the existing transaction so we
            # record it instead of re-charging on the next run.
            tx = _recover_charge(db, reference)
            if tx is None:
                logger.warning(
                    "autopay charge errored for invoice %s", invoice.id, exc_info=True
                )
                failed += 1
                continue

        if str(tx.get("status")) != "success":
            failed += 1
            continue

        try:
            billing_adapter.record_payment(
                db,
                PaymentIntent(
                    account_id=coerce_uuid(str(invoice.account_id)),
                    amount=amount,
                    currency=getattr(invoice, "currency", "NGN"),
                    status=PaymentStatus.succeeded,
                    external_id=reference,
                    memo=f"Autopay charge ref: {reference}",
                    allocations=[
                        PaymentAllocationApply(
                            invoice_id=coerce_uuid(str(invoice.id)), amount=amount
                        )
                    ],
                ),
            )
        except Exception:  # noqa: BLE001 - the card was already charged
            # The charge succeeded at the provider but recording failed. Do NOT
            # let the next run re-charge: log loudly for manual reconciliation
            # (the provider reference uniquely identifies the captured payment).
            logger.error(
                "AUTOPAY RECONCILE: charged ref=%s for invoice=%s but failed to "
                "record the payment",
                str(tx.get("reference") or reference),
                invoice.id,
                exc_info=True,
            )
            failed += 1
            continue
        charged += 1
        total += amount

    return {"charged": charged, "failed": failed, "total": total}


def run_all_due(db: Session) -> dict:
    """Entry point for the scheduled task: run autopay for every active mandate."""
    mandates = db.scalars(
        select(AutopayMandate).where(AutopayMandate.is_active.is_(True))
    ).all()
    summary = {"accounts": 0, "charged": 0, "failed": 0}
    for mandate in mandates:
        summary["accounts"] += 1
        try:
            result = run_account_autopay(db, str(mandate.account_id))
        except Exception:  # noqa: BLE001 - one account must not abort the batch
            logger.error(
                "autopay failed for account %s", mandate.account_id, exc_info=True
            )
            db.rollback()
            summary["failed"] += 1
            continue
        summary["charged"] += result.get("charged", 0)
        summary["failed"] += result.get("failed", 0)
    return summary
