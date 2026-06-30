"""Self-serve quote deposit collection via the existing billing surface.

The install deposit is collected by reusing the customer's normal invoice + pay
flow — so every configured provider (Paystack / Flutterwave / bank transfer /
saved card) works with no bespoke deposit gateway. A deposit Invoice is raised
for the quote, paid via ``create_invoice_payment_intent`` +
``verify_and_record_payment``, and on settlement the quote is accepted in the CRM
(which records the deposit and triggers the sales order + install project).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus
from app.models.quote_mirror import QuoteMirror
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import customer_portal_flow_payments as payments
from app.services import quotes_mirror
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def _quote_row(db: Session, subscriber_id: str, quote_id: str) -> QuoteMirror:
    sub_uuid = coerce_uuid(str(subscriber_id))
    row = db.scalar(
        select(QuoteMirror).where(
            QuoteMirror.crm_quote_id == str(quote_id),
            QuoteMirror.subscriber_id == sub_uuid,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    return row


def initiate_deposit(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    provider: str | None = None,
    redirect_url: str | None = None,
) -> dict:
    """Raise a deposit invoice for the quote and start its payment checkout."""
    row = _quote_row(db, subscriber_id, quote_id)
    if row.deposit_paid:
        raise HTTPException(status_code=409, detail="Deposit already paid")
    deposit = Decimal(str(row.deposit_amount or "0"))
    if deposit <= 0:
        raise HTTPException(status_code=400, detail="This quote has no deposit due")

    sub_uuid = coerce_uuid(str(subscriber_id))
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=sub_uuid,
            status=InvoiceStatus.issued,
            currency=row.currency or "NGN",
            subtotal=deposit,
            total=deposit,
            balance_due=deposit,
            issued_at=datetime.now(UTC),
            memo=f"Installation deposit · quote {quote_id}",
        ),
    )
    # Trace the deposit back to its quote for reconciliation/audit.
    invoice.metadata_ = {"quote_id": str(quote_id), "payment_flow": "quote_deposit"}
    db.commit()

    try:
        intent = payments.create_invoice_payment_intent(
            db, customer, str(invoice.id), provider=provider, redirect_url=redirect_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "invoice_id": str(invoice.id),
        "quote_id": str(quote_id),
        "amount": str(deposit),
        "currency": intent.get("currency", row.currency or "NGN"),
        "provider_type": intent.get("provider_type"),
        "provider_public_key": intent.get("provider_public_key"),
        "payment_reference": intent.get("reference"),
        "checkout_url": intent.get("checkout_url"),
        "customer_email": intent.get("customer_email"),
        "charged": bool(intent.get("charged")),
    }


def verify_deposit(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    reference: str,
    provider: str | None = None,
) -> dict:
    """Verify the deposit payment; on full settlement, accept the quote in the CRM."""
    row = _quote_row(db, subscriber_id, quote_id)
    try:
        result = payments.verify_and_record_payment(
            db, customer, reference, provider=provider
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    invoice = result.get("invoice")
    paid = (
        invoice is not None and getattr(invoice, "status", None) == InvoiceStatus.paid
    )
    if not paid:
        # Partial / pending — surface the current quote unchanged; the customer
        # can retry. (Deposits are single full payments, so this is the edge.)
        return {
            "paid": False,
            "quote": quotes_mirror._row_to_item(row),
            "reference": reference,
        }

    amount = str(result.get("amount") or row.deposit_amount or "0")
    quote = quotes_mirror.accept_quote(
        db,
        str(subscriber_id),
        str(quote_id),
        deposit_reference=reference,
        deposit_amount=amount,
        provider=provider,
    )
    return {"paid": True, "quote": quote, "reference": reference}
