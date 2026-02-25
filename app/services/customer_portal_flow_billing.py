"""Billing and arrangement flows for customer portal."""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_portal_context import (
    get_allowed_account_ids,
    get_invoice_billing_contact,
    get_outstanding_balance,
)
from app.services.customer_portal_flow_common import _compute_total_pages

logger = logging.getLogger(__name__)


def get_billing_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get billing page data for the customer portal."""
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if status == "pending":
        status = "issued"

    empty_result: dict[str, Any] = {
        "invoices": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
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

    return {
        "invoices": invoices,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
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

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

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
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    invoices: list[Any] = []
    outstanding_balance: int | float = 0
    if account_id_str:
        balance_data = get_outstanding_balance(db, account_id_str)
        invoices = balance_data["invoices"]
        outstanding_balance = balance_data["outstanding_balance"]

    selected_invoice = None
    if invoice_id:
        selected_invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)

    return {
        "invoices": invoices,
        "selected_invoice": selected_invoice,
        "outstanding_balance": outstanding_balance,
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

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None
    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    amount = Decimal(total_amount.replace(",", ""))

    if not account_id_str:
        raise ValueError("account_id is required to create a payment arrangement")

    arrangement_service.payment_arrangements.create(
        db=db,
        account_id=account_id_str,
        total_amount=amount,
        installments=installments,
        frequency=frequency,
        start_date=start,
        invoice_id=invoice_id if invoice_id else None,
        requested_by_person_id=str(subscriber.id) if subscriber else None,
        notes=notes,
    )
    return {"success": True}


def get_arrangement_error_context(
    db: Session,
    account_id_str: str | None,
) -> dict:
    """Get context data for re-rendering the arrangement form after an error."""
    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status="overdue",
        is_active=True,
        order_by="due_at",
        order_dir="asc",
        limit=50,
        offset=0,
    )
    outstanding_balance = sum(inv.balance_due or 0 for inv in invoices)
    return {"invoices": invoices, "outstanding_balance": outstanding_balance}


def get_payment_arrangement_detail(
    db: Session,
    customer: dict,
    arrangement_id: str,
) -> dict | None:
    """Get payment arrangement detail data for the customer portal."""
    from app.services import payment_arrangements as arrangement_service

    account_id = customer.get("account_id")

    try:
        arrangement = arrangement_service.payment_arrangements.get(
            db=db, arrangement_id=arrangement_id
        )
    except Exception:
        return None

    if not arrangement:
        return None

    if account_id and str(arrangement.subscriber_id) != str(account_id):
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
    allowed_account_ids = get_allowed_account_ids(customer, db)

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
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
    "get_payment_arrangement_detail",
    "get_invoice_detail",
]
