"""Reseller portal consolidated-billing flows.

Reuses the customer-portal gateway integration (Paystack/Flutterwave) so a
reseller can pay one lump sum that auto-allocates across their subscribers'
open invoices.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentStatus, TopupIntent
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.customer_portal_flow_payments import (
    _provider_uuid,
    _resolve_payment_provider,
)
from app.services.payment_gateway_adapter import payment_gateway_adapter

logger = logging.getLogger(__name__)

_INTENT_TTL = timedelta(minutes=30)


def get_billing_account_summary(db: Session, reseller_id: str) -> dict:
    """Return the consolidated-billing statement for a reseller."""
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    statement = billing_service.billing_accounts.statement(db, str(ba.id))
    return {
        "billing_account": statement.billing_account,
        "subscribers": [s.model_dump() for s in statement.subscribers],
        "recent_payments": [p.model_dump() for p in statement.recent_payments],
        "total_outstanding": statement.total_outstanding,
        "unallocated_balance": statement.unallocated_balance,
    }


def start_consolidated_payment(
    db: Session,
    reseller_id: str,
    amount: Decimal | int | float | str,
    *,
    provider: str | None = None,
) -> dict:
    """Build a gateway context + TopupIntent scoped to the reseller's BillingAccount."""
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    requested_amount = round_money(to_decimal(amount))
    if requested_amount <= Decimal("0.00"):
        raise ValueError("Payment amount must be greater than 0")

    provider_type = provider or _resolve_payment_provider(db)
    gateway_context = payment_gateway_adapter.build_context(
        db, provider_type=provider_type
    )

    intent = TopupIntent(
        billing_account_id=ba.id,
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        currency=ba.currency,
        requested_amount=requested_amount,
        status="pending",
        expires_at=datetime.now(UTC) + _INTENT_TTL,
        metadata_={"payment_flow": "reseller_consolidated"},
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)
    return {
        "intent_id": str(intent.id),
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "requested_amount": requested_amount,
        "currency": intent.currency,
        "checkout_metadata": {
            "payment_flow": "reseller_consolidated",
            "topup_intent_id": str(intent.id),
            "billing_account_id": str(ba.id),
            "reseller_id": str(reseller_id),
        },
    }


def verify_and_record_consolidated_payment(
    db: Session, reseller_id: str, reference: str, *, provider: str | None = None
) -> dict:
    """Verify a gateway payment and record it as a consolidated payment."""
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if not intent:
        raise ValueError("Payment reference was not issued for this billing account")
    if intent.billing_account_id != ba.id:
        raise ValueError("Payment reference does not belong to this billing account")

    if intent.completed_payment_id:
        payment = db.get(Payment, intent.completed_payment_id)
        return {
            "payment_id": str(payment.id) if payment else None,
            "amount": str(payment.amount) if payment else None,
            "currency": payment.currency if payment else None,
            "already_recorded": True,
        }

    provider_type = intent.provider_type or provider or _resolve_payment_provider(db)
    tx = payment_gateway_adapter.verify(
        db, provider_type=provider_type, reference=reference
    )
    amount = round_money(tx.amount)
    external_id = tx.external_id

    # Idempotency: if a payment already exists for this gateway transaction,
    # link the intent to it rather than creating a duplicate.
    existing = db.scalars(
        select(Payment).where(Payment.external_id == external_id)
    ).first()
    if existing is not None:
        intent.completed_payment_id = existing.id
        intent.completed_at = datetime.now(UTC)
        intent.status = "completed"
        intent.actual_amount = amount
        intent.external_id = external_id
        db.commit()
        return {
            "payment_id": str(existing.id),
            "amount": str(existing.amount),
            "currency": existing.currency,
            "already_recorded": True,
        }

    payment_create = PaymentCreate(
        billing_account_id=ba.id,
        amount=amount,
        currency=tx.currency,
        status=PaymentStatus.succeeded,
        provider_id=_provider_uuid(db, provider_type),
        external_id=external_id,
        memo=f"Reseller consolidated payment ref: {reference}",
        paid_at=datetime.now(UTC),
        allocations=None,
    )
    payment = billing_service.payments.create(db, payment_create)

    intent.completed_payment_id = payment.id
    intent.completed_at = datetime.now(UTC)
    intent.status = "completed"
    intent.actual_amount = amount
    intent.external_id = external_id
    db.commit()

    return {
        "payment_id": str(payment.id),
        "amount": str(payment.amount),
        "currency": payment.currency,
        "already_recorded": False,
    }


def _coerce_uuid_str(value) -> str | None:
    coerced = coerce_uuid(value)
    return str(coerced) if coerced else None


__all__ = [
    "get_billing_account_summary",
    "start_consolidated_payment",
    "verify_and_record_consolidated_payment",
]
