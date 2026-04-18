"""Online payment provider flows for customer portal."""

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.billing import InvoiceStatus, Payment, PaymentStatus
from app.models.domain_settings import SettingDomain
from app.services import billing as billing_service
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.customer_portal_context import (
    get_allowed_account_ids,
    get_invoice_billing_contact,
)
from app.services.payment_gateway_adapter import payment_gateway_adapter
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

    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
        invoice_number=invoice_number,
    )
    return {
        "invoice": invoice,
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "paystack_public_key": gateway_context.public_key
        if gateway_context.provider_type == "paystack"
        else None,
        "payment_reference": gateway_context.reference,
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

    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
    )
    invoice_id = tx.metadata.get("invoice_id")
    amount_naira = tx.amount

    if not invoice_id:
        raise ValueError("Payment metadata missing invoice_id")

    # Idempotency: check if a payment with this external reference already exists
    existing_payment = db.query(Payment).filter(Payment.external_id == tx.external_id).first()
    if existing_payment:
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        return {
            "payment": existing_payment,
            "invoice": invoice,
            "amount": getattr(existing_payment, "amount", amount_naira),
            "reference": reference,
        }

    allowed_account_ids = get_allowed_account_ids(customer, db)
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
    ):
        raise ValueError("Invoice not found or access denied")

    from uuid import UUID as _UUID

    from app.schemas.billing import PaymentAllocationApply

    payment = billing_adapter.record_payment(
        db,
        PaymentIntent(
            account_id=_UUID(str(invoice.account_id)),
            amount=amount_naira,
            currency=tx.currency,
            status=PaymentStatus.succeeded,
            external_id=tx.external_id,
            memo=f"{tx.memo_prefix} payment ref: {reference}",
            allocations=[
                PaymentAllocationApply(
                    invoice_id=_UUID(str(invoice_id)),
                    amount=amount_naira,
                )
            ],
        ),
    )

    return {
        "payment": payment,
        "invoice": invoice,
        "amount": amount_naira,
        "reference": reference,
    }


def get_topup_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the prepaid top-up page."""
    account_id = customer.get("account_id")
    provider_type = _resolve_payment_provider(db)

    # Resolve current balance
    prepaid_balance = Decimal("0.00")
    try:
        from app.services.collections._core import _resolve_prepaid_available_balance

        prepaid_balance = _resolve_prepaid_available_balance(db, str(account_id))
    except Exception:
        logger.warning(
            "Failed to resolve prepaid balance for account %s",
            account_id,
            exc_info=True,
        )

    # Top-up limits from settings
    min_amount = resolve_value(db, SettingDomain.billing, "topup_min_amount")
    max_amount = resolve_value(db, SettingDomain.billing, "topup_max_amount")
    min_amount_value = (
        int(min_amount) if isinstance(min_amount, (str, int, float)) else 1000
    )
    max_amount_value = (
        int(max_amount) if isinstance(max_amount, (str, int, float)) else 500000
    )

    email = customer.get("username", "")

    context = {
        "provider_type": provider_type,
        "customer_email": email,
        "prepaid_balance": float(prepaid_balance),
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "preset_amounts": [1000, 2000, 5000, 10000, 20000, 50000],
    }

    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
    )
    context["provider_public_key"] = gateway_context.public_key
    if gateway_context.provider_type == "paystack":
        context["paystack_public_key"] = gateway_context.public_key

    return context


def verify_and_record_topup(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify a top-up payment and add credit to account balance."""
    provider_type = provider or _resolve_payment_provider(db)

    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
    )
    amount_naira = tx.amount
    external_id = tx.external_id

    # Idempotency check
    existing = db.query(Payment).filter(Payment.external_id == external_id).first()
    if existing:
        # Payment already recorded — still attempt service restore in case
        # the prior run failed at the restore step
        account_id = customer.get("account_id")
        try:
            from app.services import collections as collections_service

            collections_service.restore_account_services(db, str(account_id))
        except Exception as exc:
            logger.warning(
                "Best-effort service restore retry failed for account %s: %s",
                account_id,
                exc,
            )
        return {
            "payment": existing,
            "amount": getattr(existing, "amount", amount_naira),
            "reference": reference,
            "already_recorded": True,
        }

    # Create unallocated payment (credit to account balance)
    from uuid import UUID as _UUID

    account_id = customer.get("account_id")
    # No explicit allocations — auto-allocation pays outstanding invoices
    # first, then remaining amount goes to account credit. This is
    # intentional: a subscriber who owes money should settle debts before
    # accumulating credit.
    payment = billing_adapter.record_payment(
        db,
        PaymentIntent(
            account_id=_UUID(str(account_id)),
            amount=amount_naira,
            currency=tx.currency,
            status=PaymentStatus.succeeded,
            external_id=external_id,
            memo=f"{tx.memo_prefix} prepaid top-up ref: {reference}",
            allocations=[],  # No invoice allocation — goes to account credit
        ),
    )

    # Emit usage_topped_up event (triggers notification + potential service restore)
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.usage_topped_up,
        {
            "account_id": str(account_id),
            "amount": str(amount_naira),
            "reference": reference,
        },
        account_id=account_id,
    )

    # Attempt to restore suspended prepaid subscriptions
    try:
        from app.services import collections as collections_service

        restored = collections_service.restore_account_services(db, str(account_id))
        if restored:
            logger.info(
                "Restored %d subscription(s) after prepaid top-up for account %s",
                restored,
                account_id,
            )
    except Exception as exc:
        logger.warning(
            "Failed to auto-restore after top-up for account %s: %s",
            account_id,
            exc,
        )

    return {
        "payment": payment,
        "amount": amount_naira,
        "reference": reference,
        "already_recorded": False,
    }


__all__ = [
    "_resolve_payment_provider",
    "get_payment_page",
    "get_topup_page",
    "verify_and_record_payment",
    "verify_and_record_topup",
]
