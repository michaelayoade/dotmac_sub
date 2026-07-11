"""Billing and arrangement flows for customer portal."""

import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNote,
    Invoice,
    InvoiceStatus,
    LedgerSource,
    Payment,
    PaymentStatus,
)
from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services.collections import get_available_balance
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_context import (
    customer_can_access_account,
    optional_customer_account_id,
    optional_customer_subscriber_id,
)
from app.services.customer_portal_context import (
    get_invoice_billing_contact,
    get_outstanding_balance,
)
from app.services.customer_portal_flow_common import _compute_total_pages

logger = logging.getLogger(__name__)

INTERNAL_LEDGER_MEMO_EXACT = {
    "Prepaid opening balance @ cutover",
}
INTERNAL_LEDGER_MEMO_PREFIXES = (
    "Correction:",
    "Partial cutover opening balance construction adjustment",
    "Reversal of phantom",
    "Reversal of prepaid opening",
    "Data repair 2026-06-29:",
    "Validated account credit consumed",
)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _money_activity_timestamp(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _customer_visible_ledger_memo(memo: str | None) -> bool:
    text = str(memo or "")
    return text not in INTERNAL_LEDGER_MEMO_EXACT and not text.startswith(
        INTERNAL_LEDGER_MEMO_PREFIXES
    )


def _build_billing_activity(db: Session, account_id: str, limit: int = 25) -> list[Any]:
    """Build the customer-visible money trail.

    The admin customer page exposes payments and credit notes directly. Some
    credit notes do not have ledger rows, so a ledger-only portal view can miss
    real account activity.
    """
    account_uuid = coerce_uuid(account_id)
    activity: list[Any] = []

    payments = (
        db.query(Payment)
        .filter(Payment.account_id == account_uuid)
        .filter(Payment.is_active.is_(True))
        # Failed/canceled payments never moved money; rendering them as
        # credit-direction "Payment update" lines reads as a double charge.
        .filter(Payment.status.notin_([PaymentStatus.failed, PaymentStatus.canceled]))
        .order_by(func.coalesce(Payment.paid_at, Payment.created_at).desc())
        .limit(limit)
        .all()
    )
    for payment in payments:
        status = _enum_value(payment.status)
        title = (
            "Payment received"
            if payment.status == PaymentStatus.succeeded
            else "Payment update"
        )
        activity.append(
            SimpleNamespace(
                kind="payment",
                payment_id=str(payment.id),
                direction="credit",
                title=title,
                description=payment.memo or status.replace("_", " ").title(),
                amount=payment.amount or Decimal("0.00"),
                currency=payment.currency or "NGN",
                occurred_at=_money_activity_timestamp(
                    payment.paid_at, payment.created_at
                ),
                reference=payment.receipt_number or payment.external_id,
            )
        )

    credit_notes = (
        db.query(CreditNote)
        .filter(CreditNote.account_id == account_uuid)
        .filter(CreditNote.is_active.is_(True))
        .order_by(CreditNote.created_at.desc())
        .limit(limit)
        .all()
    )
    for note in credit_notes:
        status = _enum_value(note.status).replace("_", " ").title()
        activity.append(
            SimpleNamespace(
                kind="credit_note",
                payment_id=None,
                direction="credit",
                title="Credit added",
                description=note.memo or status,
                amount=note.total or Decimal("0.00"),
                currency=note.currency or "NGN",
                occurred_at=_money_activity_timestamp(note.created_at),
                reference=note.credit_number,
            )
        )

    ledger_entries = billing_service.ledger_entries.list(
        db, account_id, None, None, True, "effective_date", "desc", limit, 0
    )
    for entry in ledger_entries:
        source = _enum_value(entry.source)
        if source in {LedgerSource.payment.value, LedgerSource.credit_note.value}:
            continue
        if not _customer_visible_ledger_memo(entry.memo):
            continue
        entry_type = _enum_value(entry.entry_type)
        category = _enum_value(entry.category).replace("_", " ").title()
        activity.append(
            SimpleNamespace(
                kind=source or "ledger",
                payment_id=None,
                direction=entry_type,
                title=entry.memo or category or source.replace("_", " ").title(),
                description=category or source.replace("_", " ").title(),
                amount=entry.amount or Decimal("0.00"),
                currency=entry.currency or "NGN",
                occurred_at=_money_activity_timestamp(
                    entry.effective_date, entry.created_at
                ),
                reference=None,
            )
        )

    activity.sort(
        key=lambda item: item.occurred_at or datetime.min,
        reverse=True,
    )
    return activity[:limit]


def get_billing_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get billing page data for the customer portal."""
    account_id_str = optional_customer_account_id(db, customer)

    if status == "pending":
        status = "issued"

    empty_result: dict[str, Any] = {
        "invoices": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
        "prepaid_balance": None,
        "ledger_entries": [],
        "billing_activity": [],
    }
    if not account_id_str:
        return empty_result

    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        is_active=None,
        order_by="issued_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = (
        select(func.count(Invoice.id))
        .where(Invoice.account_id == coerce_uuid(account_id_str))
        .where(Invoice.is_active.is_(True))
    )
    if status:
        stmt = stmt.where(
            Invoice.status == _validate_enum(status, InvoiceStatus, "status")
        )
    total = db.scalar(stmt) or 0
    prepaid_balance: Decimal | None = None
    try:
        prepaid_balance = get_available_balance(db, account_id_str)
    except Exception:
        logger.warning(
            "Failed to resolve prepaid balance for billing page account %s",
            account_id_str,
            exc_info=True,
        )

    # Backwards-compatible raw ledger rows for older templates.
    ledger_entries = billing_service.ledger_entries.list(
        db, account_id_str, None, None, True, "effective_date", "desc", 25, 0
    )
    billing_activity = _build_billing_activity(db, account_id_str)

    return {
        "invoices": invoices,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
        "prepaid_balance": prepaid_balance,
        "ledger_entries": ledger_entries,
        "billing_activity": billing_activity,
    }


def get_payment_arrangements_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get payment arrangements page data for the customer portal."""
    from app.services import payment_arrangements as arrangement_service

    account_id_str = optional_customer_account_id(db, customer)

    empty_result: dict[str, Any] = {
        "arrangements": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(PaymentArrangement.id)).where(
        PaymentArrangement.is_active.is_(True)
    )
    if account_id_str:
        stmt = stmt.where(
            PaymentArrangement.subscriber_id == coerce_uuid(account_id_str)
        )
    if status:
        stmt = stmt.where(
            PaymentArrangement.status
            == _validate_enum(status, ArrangementStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "arrangements": arrangements,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_new_arrangement_page(
    db: Session,
    customer: dict,
    invoice_id: str | None = None,
) -> dict:
    """Get data for the new payment arrangement form."""
    account_id_str = optional_customer_account_id(db, customer)

    invoices: list[Any] = []
    outstanding_balance: int | float = 0
    if account_id_str:
        balance_data = get_outstanding_balance(db, account_id_str)
        invoices = balance_data["invoices"]
        outstanding_balance = balance_data["outstanding_balance"]

    selected_invoice = None
    if invoice_id:
        try:
            candidate_invoice = billing_service.invoices.get(
                db=db, invoice_id=invoice_id
            )
        except Exception:
            candidate_invoice = None
        if candidate_invoice and customer_can_access_account(
            db, customer, getattr(candidate_invoice, "account_id", None)
        ):
            selected_invoice = candidate_invoice

    # Eligibility: the form is only usable when there is an overdue balance to
    # arrange. Without this the template's `{% if not eligible %}` always fired,
    # so every customer saw "Not Eligible" and the form never rendered.
    overdue_invoices = invoices
    eligible = (
        bool(account_id_str) and outstanding_balance and len(overdue_invoices) > 0
    )
    ineligible_reason = (
        None
        if eligible
        else "You have no overdue balance that requires a payment arrangement."
    )
    due_dates = [inv.due_at for inv in overdue_invoices if getattr(inv, "due_at", None)]
    oldest_due_date = min(due_dates) if due_dates else None

    return {
        "invoices": invoices,
        "overdue_invoices": overdue_invoices,
        "selected_invoice": selected_invoice,
        "outstanding_balance": outstanding_balance,
        "eligible": bool(eligible),
        "ineligible_reason": ineligible_reason,
        "oldest_due_date": oldest_due_date,
    }


def submit_payment_arrangement(
    db: Session,
    customer: dict,
    total_amount: str,
    installments: int,
    frequency: str,
    start_date: str,
    invoice_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Submit a payment arrangement request."""
    from app.services import payment_arrangements as arrangement_service

    account_id_str = optional_customer_account_id(db, customer)
    subscriber_id = optional_customer_subscriber_id(db, customer)
    subscriber = (
        db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
    )

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    amount = Decimal(total_amount.replace(",", ""))

    if not account_id_str:
        raise ValueError("account_id is required to create a payment arrangement")

    arrangement_service.payment_arrangements.create(
        db=db,
        subscriber_id=account_id_str,
        total_amount=amount,
        installments=installments,
        frequency=frequency,
        start_date=start,
        invoice_id=invoice_id if invoice_id else None,
        requested_by_subscriber_id=str(subscriber.id) if subscriber else None,
        notes=notes,
    )
    return {"success": True}


def get_arrangement_error_context(
    db: Session,
    account_id_str: str | None,
) -> dict:
    """Get context data for re-rendering the arrangement form after an error."""
    balance_data: dict[str, Any] = (
        get_outstanding_balance(db, account_id_str)
        if account_id_str
        else {"invoices": [], "outstanding_balance": 0}
    )
    invoices = cast(list[Invoice], balance_data["invoices"])
    outstanding_balance = balance_data["outstanding_balance"]
    # The form template needs the same eligibility fields as the GET page,
    # otherwise the error re-render falls into the "Not Eligible" branch.
    eligible = bool(account_id_str) and outstanding_balance and len(invoices) > 0
    due_dates = [due_at for inv in invoices if (due_at := inv.due_at) is not None]
    return {
        "invoices": invoices,
        "overdue_invoices": invoices,
        "selected_invoice": None,
        "outstanding_balance": outstanding_balance,
        "eligible": bool(eligible),
        "ineligible_reason": None
        if eligible
        else "You have no overdue balance that requires a payment arrangement.",
        "oldest_due_date": min(due_dates) if due_dates else None,
    }


def cancel_customer_arrangement(
    db: Session,
    customer: dict,
    arrangement_id: str,
) -> dict:
    """Cancel the customer's own arrangement.

    Customers may only cancel arrangements that are still PENDING approval.
    Once an arrangement has been approved (active) — or has progressed
    further — cancellation requires an admin.

    Raises:
        HTTPException 404 when the arrangement is missing or not theirs,
        HTTPException 400 when the arrangement is not pending.
    """
    from app.services import payment_arrangements as arrangement_service

    account_id = optional_customer_account_id(db, customer)
    arrangement = arrangement_service.payment_arrangements.get(db, arrangement_id)
    if not account_id or str(arrangement.subscriber_id) != str(account_id):
        raise HTTPException(status_code=404, detail="Payment arrangement not found")

    if arrangement.status != ArrangementStatus.pending:
        raise HTTPException(
            status_code=400,
            detail=(
                "Only pending arrangements can be canceled. Please contact "
                "support to cancel an approved arrangement."
            ),
        )

    arrangement_service.payment_arrangements.cancel(
        db, arrangement_id, notes="Canceled by customer via portal"
    )
    return {"success": True}


def get_payment_arrangement_detail(
    db: Session,
    customer: dict,
    arrangement_id: str,
) -> dict | None:
    """Get payment arrangement detail data for the customer portal."""
    from app.services import payment_arrangements as arrangement_service

    account_id = optional_customer_account_id(db, customer)

    try:
        arrangement = arrangement_service.payment_arrangements.get(
            db=db, arrangement_id=arrangement_id
        )
    except Exception:
        return None

    if not arrangement:
        return None

    if not account_id or str(arrangement.subscriber_id) != str(account_id):
        return None

    installments = arrangement_service.installments.list(
        db=db,
        arrangement_id=arrangement_id,
        status=None,
        order_by="installment_number",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    return {
        "arrangement": arrangement,
        "installments": installments,
    }


def get_invoice_detail(
    db: Session,
    customer: dict,
    invoice_id: str,
) -> dict | None:
    """Get invoice detail data for the customer portal."""
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        return None

    billing_contact = get_invoice_billing_contact(db, invoice, customer)

    return {
        "invoice": invoice,
        "billing_name": billing_contact["billing_name"],
        "billing_email": billing_contact["billing_email"],
    }


__all__ = [
    "get_billing_page",
    "get_payment_arrangements_page",
    "get_new_arrangement_page",
    "submit_payment_arrangement",
    "get_arrangement_error_context",
    "cancel_customer_arrangement",
    "get_payment_arrangement_detail",
    "get_invoice_detail",
]
