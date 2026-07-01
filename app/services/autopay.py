"""Autopay engine — auto-charge a saved card on due invoices.

A customer opts in (one mandate per account, pointing at a saved card). The
Celery task calls :func:`run_all_due`, which for each active mandate charges the
saved card via Paystack ``charge_authorization`` for due open invoices and
records the payment through the existing billing adapter. All charges are
server-to-server; nothing here needs the customer present.

Safety properties:
- Per-account advisory lock serializes concurrent runs.
- Charge references are deterministic per (invoice, amount, attempt); within an
  attempt overlapping runs resolve to the SAME Paystack transaction.
- A decline burns the reference at Paystack, so the next attempt (tracked by the
  mandate's ``failure_count``) uses a fresh reference — declines stay retryable.
- Before charging, any already-succeeded autopay payment for the same
  (invoice, amount) is detected (DB check across attempts + provider-side
  recovery of prior attempt references) so the card is never captured twice.
- Mandates are suspended after the configured max failed runs and the
  customer is notified on every failed charge via the payment_failed event.
"""

from __future__ import annotations

import builtins
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
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
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber
from app.schemas.billing import PaymentAllocationApply
from app.services import billing as billing_service
from app.services import paystack, settings_spec
from app.services.billing._common import lock_account
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.billing_settings import account_has_collectible_service
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.events import emit_event
from app.services.events.types import EventType

logger = logging.getLogger(__name__)

_OPEN_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)

# Default max consecutive failed runs before the customer must re-enable autopay
# or pick a new default card to reset the counter.
MAX_CONSECUTIVE_FAILURES = 3


def _max_consecutive_failures(db: Session) -> int:
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "autopay_max_consecutive_failures"
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return MAX_CONSECUTIVE_FAILURES
    return max(1, parsed)


def max_consecutive_failures(db: Session) -> int:
    """Configured autopay failure cap used by both charging and admin views."""
    return _max_consecutive_failures(db)


def _mandate(db: Session, account_id: str) -> AutopayMandate | None:
    # The topup page can be reached with an unresolved/non-UUID account id
    # (impersonation token, partial session); a lookup with no valid id simply
    # has no mandate rather than crashing the page.
    try:
        account_uuid = coerce_uuid(account_id)
    except (ValueError, AttributeError, TypeError):
        return None
    if account_uuid is None:
        return None
    return db.scalars(
        select(AutopayMandate).where(AutopayMandate.account_id == account_uuid)
    ).first()


def get_status(db: Session, account_id: str) -> dict:
    m = _mandate(db, account_id)
    failure_count = int(getattr(m, "failure_count", 0) or 0) if m else 0
    return {
        "enabled": bool(m and m.is_active),
        "payment_method_id": str(m.payment_method_id)
        if m and m.payment_method_id
        else None,
        "failure_count": failure_count,
        "last_failure_at": m.last_failure_at if m else None,
        "last_failure_reason": m.last_failure_reason if m else None,
        # Active but no longer charged until the customer intervenes.
        "suspended": bool(
            m and m.is_active and failure_count >= _max_consecutive_failures(db)
        ),
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
    # An explicit (re-)enable is customer intent to try again: clear declines.
    m.failure_count = 0
    m.last_failure_at = None
    m.last_failure_reason = None
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


def reset_failures(db: Session, account_id: str) -> None:
    """Clear the decline counter (e.g. after the customer picks a new card)."""
    m = _mandate(db, account_id)
    if m is None:
        return
    m.failure_count = 0
    m.last_failure_at = None
    m.last_failure_reason = None
    db.commit()


def _charge_only_due(db: Session) -> bool:
    """Whether autopay should wait for the due date (default) or charge at
    issuance (legacy behaviour, restorable via BILLING_AUTOPAY_CHARGE_ONLY_DUE)."""
    value = settings_spec.resolve_value(
        db, SettingDomain.billing, "autopay_charge_only_due"
    )
    return value is not False


def _open_invoices(db: Session, account_id: str) -> builtins.list[Invoice]:
    query = select(Invoice).where(
        Invoice.account_id == coerce_uuid(account_id),
        Invoice.is_active.is_(True),
        Invoice.status.in_(_OPEN_STATUSES),
        Invoice.balance_due > 0,
    )
    if _charge_only_due(db):
        # Only invoices that are actually due: past their due date, or flagged
        # overdue without a due date. Freshly issued, not-yet-due invoices wait.
        query = query.where(
            or_(
                Invoice.due_at <= datetime.now(UTC),
                and_(
                    Invoice.due_at.is_(None),
                    Invoice.status == InvoiceStatus.overdue,
                ),
            )
        )
    return list(db.scalars(query).all())


def _email(db: Session, account_id: str) -> str:
    subscriber = db.get(Subscriber, coerce_uuid(account_id))
    return str(getattr(subscriber, "email", "") or "")


def _reference_base(invoice_id: str, amount_kobo: int) -> str:
    return f"AUTOPAY-{invoice_id}-{amount_kobo}"


def _autopay_reference(invoice_id: str, amount_kobo: int, attempt: int = 0) -> str:
    """Deterministic provider reference for one (invoice, amount, attempt) charge.

    Within an attempt the reference is stable, so overlapping runs or a retry
    after a record failure resolve to the SAME Paystack transaction instead of
    capturing the card twice. A DECLINE burns the reference at Paystack, so the
    next attempt (the mandate's failure_count) gets a fresh ``-A{n}`` suffix —
    otherwise one decline would block autopay for that invoice forever. The
    amount is in the key so a legitimately re-opened invoice (e.g. after a
    partial payment) gets a fresh reference. Attempt 0 keeps the legacy,
    suffix-free format for continuity with already-issued references.
    """
    base = _reference_base(invoice_id, amount_kobo)
    return base if attempt <= 0 else f"{base}-A{attempt}"


def _succeeded_autopay_payment(
    db: Session, invoice_id: str, amount_kobo: int
) -> Payment | None:
    """A SUCCEEDED autopay payment already recorded for this (invoice, amount),
    at any attempt number."""
    base = _reference_base(invoice_id, amount_kobo)
    return db.scalars(
        select(Payment).where(
            or_(
                Payment.external_id == base,
                Payment.external_id.like(f"{base}-A%"),
            ),
            Payment.status == PaymentStatus.succeeded,
        )
    ).first()


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


def _failure_reason(tx: dict[str, Any] | None) -> str:
    if tx is None:
        return "charge_error"
    for key in ("gateway_response", "message", "status"):
        value = tx.get(key)
        if value:
            return str(value)[:255]
    return "charge_failed"


def _notify_charge_failed(
    db: Session, invoice: Invoice, amount: Decimal, reason: str
) -> None:
    """Queue the customer-facing payment-failed notification (best-effort)."""
    try:
        emit_event(
            db,
            EventType.payment_failed,
            {
                "amount": str(amount),
                "currency": getattr(invoice, "currency", "NGN"),
                "invoice_id": str(invoice.id),
                "reason": reason,
                "source": "autopay",
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )
    except Exception:  # noqa: BLE001 - notification must never abort the run
        logger.warning(
            "autopay failure notification errored for invoice %s",
            invoice.id,
            exc_info=True,
        )


def _record_failure_run(db: Session, mandate: AutopayMandate, reason: str) -> None:
    mandate.failure_count = int(mandate.failure_count or 0) + 1
    mandate.last_failure_at = datetime.now(UTC)
    mandate.last_failure_reason = reason[:255]
    db.commit()


def _record_success_run(db: Session, mandate: AutopayMandate) -> None:
    if not (
        mandate.failure_count or mandate.last_failure_at or mandate.last_failure_reason
    ):
        return
    mandate.failure_count = 0
    mandate.last_failure_at = None
    mandate.last_failure_reason = None
    db.commit()


def run_account_autopay(db: Session, account_id: str) -> dict:
    """Charge the account's saved card for each due open invoice. Charges are
    idempotent on a deterministic per-(invoice, amount, attempt) reference, so
    overlapping runs and record-failure retries never capture the card twice."""
    mandate = _mandate(db, account_id)
    if mandate is None or not mandate.is_active:
        return {"charged": 0, "failed": 0, "skipped": "not_enabled"}

    # Never auto-charge an account whose services are all terminal
    # (stopped/disabled/canceled/expired/…). A dead service shouldn't keep
    # capturing the saved card even if an open balance and mandate linger.
    # ``blocked`` is intentionally still collectible — a blocked account is a
    # recoverable non-payment hold, and auto-charging it is exactly how it gets
    # back to good standing (see COLLECTIBLE_SERVICE_STATUSES).
    if not account_has_collectible_service(db, coerce_uuid(account_id)):
        return {"charged": 0, "failed": 0, "skipped": "no_live_service"}

    attempt = int(mandate.failure_count or 0)
    if attempt >= _max_consecutive_failures(db):
        return {"charged": 0, "failed": 0, "skipped": "too_many_failures"}

    # Serialize concurrent runs for this account so two runs can't both charge
    # the same invoice before either records.
    lock_account(db, account_id)

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
    last_reason = ""

    for invoice in _open_invoices(db, account_id):
        amount = round_money(to_decimal(getattr(invoice, "balance_due", 0)))
        if amount <= Decimal("0.00"):
            continue
        amount_kobo = paystack.amount_to_kobo(amount)
        reference = _autopay_reference(str(invoice.id), amount_kobo, attempt)

        # Already charged + recorded this exact (invoice, amount) — at this or
        # any previous attempt? Skip; never capture the card twice for the
        # same balance. (Exact-reference check covers non-succeeded rows too,
        # e.g. a webhook-recorded pending payment for this attempt.)
        if _succeeded_autopay_payment(
            db, str(invoice.id), amount_kobo
        ) or _already_recorded(db, reference):
            continue

        tx: dict[str, Any] | None = None

        # A prior attempt may have captured without us confirming it (charge
        # call errored after capture). Recover it instead of charging again.
        for prior_attempt in range(attempt - 1, -1, -1):
            prior_reference = _autopay_reference(
                str(invoice.id), amount_kobo, prior_attempt
            )
            recovered = _recover_charge(db, prior_reference)
            if recovered is not None:
                tx = recovered
                reference = prior_reference
                break

        if tx is None:
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
                # The charge may have actually gone through (e.g. a
                # duplicate-reference error after a prior capture). Recover the
                # existing transaction so we record it instead of re-charging
                # on the next run.
                tx = _recover_charge(db, reference)

        if tx is None or str(tx.get("status")) != "success":
            reason = _failure_reason(tx)
            if tx is None:
                logger.warning("autopay charge errored for invoice %s", invoice.id)
            failed += 1
            last_reason = reason
            _notify_charge_failed(db, invoice, amount, reason)
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
            last_reason = "record_failed"
            continue
        charged += 1
        total += amount

    # Decline tracking: a run with any failed charge bumps the consecutive
    # failure counter (advancing the attempt suffix for the next run); a clean
    # run with at least one successful charge resets it.
    if failed:
        _record_failure_run(db, mandate, last_reason or "charge_failed")
    elif charged:
        _record_success_run(db, mandate)

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
