"""Self-serve quote deposit collection via the existing billing surface.

The install deposit is collected by reusing the customer's normal invoice + pay
flow — so every configured provider (Paystack / Flutterwave / bank transfer /
saved card) works with no bespoke deposit gateway. A deposit Invoice is raised
for the quote, paid via ``create_invoice_payment_intent`` +
``verify_and_record_payment``, and on settlement the quote is accepted — which
records the deposit and triggers the sales order + install project.

The quote is always accepted in Sub's native sales domain. CRM mirrors remain
read-only migration inputs and are never used as a write target.

Billing-safety invariant (risk #2): on either path the sole ledger event per
deposit is ``verify_and_record_payment`` on the deposit invoice; the accept
only marks the sales order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services import customer_portal_flow_payments as payments
from app.services.common import coerce_uuid
from app.services.sales import selfserve

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
    quote = selfserve.selfserve_quotes.get_for_subscriber(db, subscriber_id, quote_id)
    portal_quote = selfserve.build_portal_quote_payload(db, quote)
    if portal_quote["deposit_paid"]:
        raise HTTPException(status_code=409, detail="Deposit already paid")
    deposit = Decimal(str(portal_quote["deposit_amount"] or "0"))
    if deposit <= 0:
        raise HTTPException(status_code=400, detail="This quote has no deposit due")

    sub_uuid = coerce_uuid(str(subscriber_id))
    invoice = billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=sub_uuid,
            status=InvoiceStatus.issued,
            currency=quote.currency or "NGN",
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
        "currency": intent.get("currency", quote.currency or "NGN"),
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
    """Verify the deposit payment and accept the native quote on settlement."""
    return _verify_deposit_native(
        db,
        customer,
        subscriber_id,
        quote_id,
        reference=reference,
        provider=provider,
    )


def _verify_deposit_native(
    db: Session,
    customer: dict,
    subscriber_id: str,
    quote_id: str,
    *,
    reference: str,
    provider: str | None = None,
) -> dict:
    """Native tail (§2.2 step 4): verify the payment, then accept the quote
    in sub's own sales vertical — no CRM hop."""
    quote = selfserve.selfserve_quotes.get_for_subscriber(db, subscriber_id, quote_id)
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
        return {
            "paid": False,
            "quote": selfserve.build_portal_quote_payload(db, quote),
            "reference": reference,
        }

    current = selfserve.build_portal_quote_payload(db, quote)
    amount = str(result.get("amount") or current.get("deposit_amount") or "0")
    payload = selfserve.selfserve_quotes.accept_with_deposit(
        db,
        str(subscriber_id),
        str(quote_id),
        deposit_reference=reference,
        deposit_amount=amount,
        provider=provider,
    )
    return {"paid": True, "quote": payload, "reference": reference}
