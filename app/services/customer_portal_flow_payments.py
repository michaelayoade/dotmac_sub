"""Online payment provider flows for customer portal."""

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus
from app.models.domain_settings import SettingDomain
from app.services import billing as billing_service
from app.services.customer_portal_context import (
    get_allowed_account_ids,
    get_invoice_billing_contact,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _resolve_payment_provider(db: Session) -> str:
    """Return the configured payment provider type ('paystack' or 'flutterwave')."""
    val = resolve_value(db, SettingDomain.billing, "default_payment_provider_type")
    if val and str(val) == "flutterwave":
        return "flutterwave"
    return "paystack"


def get_payment_page(
    db: Session,
    customer: dict,
    invoice_id: str,
) -> dict | None:
    """Build context for the online payment page."""
    allowed_account_ids = get_allowed_account_ids(customer, db)

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
    ):
        return None

    if invoice.status in (InvoiceStatus.paid, InvoiceStatus.void):
        return None

    provider_type = _resolve_payment_provider(db)
    invoice_number = getattr(invoice, "invoice_number", None)

    billing_contact = get_invoice_billing_contact(db, invoice, customer)
    email = billing_contact["billing_email"] or customer.get("username", "")

    if provider_type == "flutterwave":
        from app.services.flutterwave import generate_reference, get_public_key

        reference = generate_reference(invoice_number)
        return {
            "invoice": invoice,
            "provider_type": "flutterwave",
            "provider_public_key": get_public_key(db),
            "payment_reference": reference,
            "customer_email": email,
        }

    from app.services.paystack import generate_reference, get_public_key

    reference = generate_reference(invoice_number)
    return {
        "invoice": invoice,
        "provider_type": "paystack",
        "provider_public_key": get_public_key(db),
        "paystack_public_key": get_public_key(db),
        "payment_reference": reference,
        "customer_email": email,
    }


def verify_and_record_payment(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify an online payment transaction and record the payment."""
    provider_type = provider or _resolve_payment_provider(db)

    if provider_type == "flutterwave":
        from app.services import flutterwave as flutterwave_svc

        tx = flutterwave_svc.verify_transaction(db, reference)

        if tx.get("status") != "successful":
            raise ValueError(
                f"Payment was not successful (status: {tx.get('status')})"
            )

        metadata = tx.get("meta") or {}
        invoice_id = metadata.get("invoice_id")
        amount_naira = Decimal(str(tx.get("amount", 0)))
        memo_prefix = "Flutterwave"
    else:
        from app.services.paystack import kobo_to_naira, verify_transaction

        tx = verify_transaction(db, reference)

        if tx.get("status") != "success":
            raise ValueError(
                f"Payment was not successful (status: {tx.get('status')})"
            )

        metadata = tx.get("metadata") or {}
        invoice_id = metadata.get("invoice_id")
        amount_naira = kobo_to_naira(tx.get("amount", 0))
        memo_prefix = "Paystack"

    if not invoice_id:
        raise ValueError("Payment metadata missing invoice_id")

    allowed_account_ids = get_allowed_account_ids(customer, db)
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
    ):
        raise ValueError("Invoice not found or access denied")

    from uuid import UUID as _UUID

    from app.models.billing import PaymentStatus
    from app.schemas.billing import PaymentAllocationApply, PaymentCreate

    payment_payload = PaymentCreate(
        account_id=_UUID(str(invoice.account_id)),
        amount=amount_naira,
        currency=tx.get("currency", "NGN"),
        status=PaymentStatus.succeeded,
        external_id=str(tx.get("id", "")),
        memo=f"{memo_prefix} payment ref: {reference}",
        allocations=[
            PaymentAllocationApply(
                invoice_id=_UUID(str(invoice_id)),
                amount=amount_naira,
            )
        ],
    )
    payment = billing_service.payments.create(db, payment_payload)

    return {
        "payment": payment,
        "invoice": invoice,
        "amount": amount_naira,
        "reference": reference,
    }


__all__ = [
    "_resolve_payment_provider",
    "get_payment_page",
    "verify_and_record_payment",
]
