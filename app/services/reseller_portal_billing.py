"""Reseller portal consolidated-billing flows.

Reuses the customer-portal gateway integration (Paystack/Flutterwave) so a
reseller can pay one lump sum into unallocated credit, then choose which
subscribers receive allocations.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    TopupIntent,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.customer_portal_flow_payments import (
    _provider_uuid,
    _resolve_payment_provider,
)
from app.services.payment_gateway_adapter import payment_gateway_adapter

logger = logging.getLogger(__name__)

_INTENT_TTL = timedelta(minutes=30)


def get_billing_account_summary(
    db: Session, reseller_id: str, subscriber_search: str | None = None
) -> dict:
    """Return the consolidated-billing statement for a reseller."""
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    statement = billing_service.billing_accounts.statement(
        db, str(ba.id), subscriber_search=subscriber_search
    )
    return {
        "billing_account": statement.billing_account,
        "subscribers": [s.model_dump() for s in statement.subscribers],
        "recent_payments": [p.model_dump() for p in statement.recent_payments],
        "total_outstanding": statement.total_outstanding,
        "unallocated_balance": statement.unallocated_balance,
        "subscriber_search": (subscriber_search or "").strip(),
    }


def _login_subscriber_email(db: Session, login_subscriber_id: str | None) -> str:
    """Resolve a real email for the reseller's login subscriber (gateway charge)."""
    if not login_subscriber_id:
        return ""
    coerced = coerce_uuid(login_subscriber_id)
    if not coerced:
        return ""
    subscriber = db.get(Subscriber, coerced)
    value = str(getattr(subscriber, "email", "") or "").strip() if subscriber else ""
    return value if "@" in value else ""


def start_consolidated_payment(
    db: Session,
    reseller_id: str,
    amount: Decimal | int | float | str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    save_card: bool = False,
    login_subscriber_id: str | None = None,
) -> dict:
    """Build a gateway context + TopupIntent scoped to the reseller's BillingAccount.

    Saved cards (``payment_method_id``) and ``save_card`` are keyed on the
    reseller's *login subscriber* (``login_subscriber_id``) — same account the
    customer saved-card flow uses — so a reseller's stored card token is charged
    server-to-server, and the verify path captures a new card afterwards.
    """
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    requested_amount = round_money(to_decimal(amount))
    if requested_amount <= Decimal("0.00"):
        raise ValueError("Payment amount must be greater than 0")

    provider_type = provider or _resolve_payment_provider(db)

    selected_payment_method_id = str(payment_method_id or "").strip() or None
    selected_payment_token = None
    if selected_payment_method_id:
        if provider_type != "paystack":
            raise ValueError("Saved cards can only be used with Paystack")
        if not login_subscriber_id:
            raise ValueError("Payment method not found")
        method = customer_cards._owned(
            db, str(login_subscriber_id), selected_payment_method_id
        )
        if method is None:
            raise ValueError("Payment method not found")
        selected_payment_token = billing_service.payment_methods.get_decrypted_token(
            db, str(method.id)
        )
        if not selected_payment_token:
            raise ValueError("Payment method is not chargeable")

    gateway_context = payment_gateway_adapter.build_context(
        db, provider_type=provider_type
    )

    intent_metadata = {"payment_flow": "reseller_consolidated"}
    if save_card and login_subscriber_id:
        intent_metadata["save_card"] = "1"
        intent_metadata["login_subscriber_id"] = str(login_subscriber_id)
    if selected_payment_method_id:
        intent_metadata["payment_method_id"] = selected_payment_method_id

    intent = TopupIntent(
        billing_account_id=ba.id,
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        currency=ba.currency,
        requested_amount=requested_amount,
        status="pending",
        expires_at=datetime.now(UTC) + _INTENT_TTL,
        metadata_=intent_metadata,
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)

    checkout_metadata = {
        "payment_flow": "reseller_consolidated",
        "topup_intent_id": str(intent.id),
        "billing_account_id": str(ba.id),
        "reseller_id": str(reseller_id),
        **(
            {"payment_method_id": selected_payment_method_id}
            if selected_payment_method_id
            else {}
        ),
    }

    charged = False
    if selected_payment_token is not None:
        from app.services import paystack

        paystack.charge_authorization(
            db,
            authorization_code=selected_payment_token,
            email=_login_subscriber_email(db, login_subscriber_id),
            amount_kobo=paystack.amount_to_kobo(requested_amount),
            reference=gateway_context.reference,
            metadata=checkout_metadata,
        )
        charged = True

    return {
        "intent_id": str(intent.id),
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "requested_amount": requested_amount,
        "currency": intent.currency,
        "checkout_metadata": checkout_metadata,
        "charged": charged,
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
    payment = billing_service.payments.create(db, payment_create, auto_allocate=False)

    intent.completed_payment_id = payment.id
    intent.completed_at = datetime.now(UTC)
    intent.status = "completed"
    intent.actual_amount = amount
    intent.external_id = external_id
    db.commit()

    _maybe_capture_card(db, intent, reference, provider_type)

    return {
        "payment_id": str(payment.id),
        "amount": str(payment.amount),
        "currency": payment.currency,
        "already_recorded": False,
    }


def allocate_unallocated_to_subscriber(
    db: Session, reseller_id: str, subscriber_id: str
) -> dict:
    """Apply reseller unallocated credit to one selected subscriber."""
    from fastapi import HTTPException

    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    try:
        return billing_service.payments.allocate_consolidated_balance_to_subscriber(
            db, str(ba.id), subscriber_id
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Allocation failed"
        raise ValueError(detail) from exc


def _maybe_capture_card(
    db: Session, intent: TopupIntent, reference: str, provider_type: str | None
) -> None:
    """Capture the reseller's card after a save-card consolidated payment.

    Best-effort and keyed on the login subscriber recorded at intent time, so a
    captured card lands in the same saved-card store the reseller payment-methods
    page reads. Any failure is swallowed (the payment already succeeded)."""
    metadata = dict(intent.metadata_ or {})
    if str(metadata.get("save_card") or "") != "1":
        return
    login_subscriber_id = metadata.get("login_subscriber_id")
    if not login_subscriber_id:
        return
    try:
        customer_cards.capture_card_after_payment(
            db, str(login_subscriber_id), reference, provider_type
        )
    except Exception:  # noqa: BLE001 - capture is non-critical
        logger.warning("reseller card capture skipped for %s", reference, exc_info=True)


def _coerce_uuid_str(value) -> str | None:
    coerced = coerce_uuid(value)
    return str(coerced) if coerced else None


# --- Saved cards (keyed on the reseller's login subscriber) ---------------------
#
# A reseller's saved cards live in the same PaymentMethod store the customer
# saved-card flow uses, keyed on the login Subscriber's id (not the BillingAccount).
# These thin wrappers force that scoping so the web routes and the mobile API
# share one self-scoped implementation.


def list_payment_methods(db: Session, login_subscriber_id: str | None) -> list:
    from app.models.billing import PaymentMethodType

    # Saved cards are keyed on the login Subscriber. A first-class reseller_user
    # principal (Layer 3) has no backing subscriber, so it has no saved-card
    # store yet — return empty rather than error. (Re-keying the card store to
    # reseller_user/reseller is a tracked follow-up.)
    if not login_subscriber_id:
        return []
    cards = customer_cards.list_for_account(db, str(login_subscriber_id))
    return [c for c in cards if c.method_type == PaymentMethodType.card]


def payment_method_api_dict(method) -> dict:
    return {
        "id": str(method.id),
        "label": method.label
        or (
            f"{method.brand or 'Card'}"
            + (f" •••• {method.last4}" if method.last4 else "")
        ),
        "brand": method.brand,
        "last4": method.last4,
        "expires_month": method.expires_month,
        "expires_year": method.expires_year,
        "is_default": bool(method.is_default),
    }


def set_default_payment_method(
    db: Session, login_subscriber_id: str | None, method_id: str
) -> bool:
    if not login_subscriber_id:
        return False
    return (
        customer_cards.set_default(db, str(login_subscriber_id), method_id) is not None
    )


def remove_payment_method(
    db: Session, login_subscriber_id: str | None, method_id: str
) -> bool:
    if not login_subscriber_id:
        return False
    return customer_cards.remove(db, str(login_subscriber_id), method_id)


def get_payment_methods_page(
    db: Session, reseller_id: str, login_subscriber_id: str | None
) -> dict:
    """Context for the reseller payment-methods management page."""
    summary = get_billing_account_summary(db, reseller_id)
    return {
        "saved_cards": list_payment_methods(db, login_subscriber_id),
        "provider_type": _resolve_payment_provider(db),
        "total_outstanding": summary["total_outstanding"],
        "billing_account": summary["billing_account"],
    }


def _activity_subscriber_name(subscriber: Subscriber) -> str:
    return (
        subscriber.display_name
        or subscriber.company_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.email
        or str(subscriber.id)
    )


def account_activity(
    db: Session, reseller_id: str, summary: dict | None = None, *, limit: int = 50
) -> list[dict]:
    """Return consolidated payment and allocation activity for a reseller."""
    ba = billing_service.billing_accounts.get_for_reseller(db, reseller_id)
    entries: list[dict] = []
    payment_allocated_sq = (
        db.query(
            PaymentAllocation.payment_id.label("payment_id"),
            func.coalesce(func.sum(PaymentAllocation.amount), Decimal("0.00")).label(
                "allocated_total"
            ),
        )
        .group_by(PaymentAllocation.payment_id)
        .subquery()
    )
    payments = (
        db.query(
            Payment,
            func.coalesce(
                payment_allocated_sq.c.allocated_total, Decimal("0.00")
            ).label("allocated_total"),
        )
        .outerjoin(
            payment_allocated_sq, payment_allocated_sq.c.payment_id == Payment.id
        )
        .filter(Payment.billing_account_id == ba.id)
        .filter(Payment.is_active.is_(True))
        .order_by(Payment.created_at.desc())
        .limit(limit)
        .all()
    )
    for payment, allocated_total in payments:
        unallocated = round_money(
            to_decimal(payment.amount) - to_decimal(allocated_total)
        )
        description = payment.memo or "Payment added to reseller unallocated credit"
        if unallocated > 0:
            description = (
                f"{description} · {payment.currency} {unallocated:,.2f} unallocated"
            )
        entries.append(
            {
                "direction": "credit",
                "title": "Consolidated payment",
                "description": description,
                "occurred_at": payment.paid_at or payment.created_at,
                "reference": None,
                "amount": payment.amount,
                "currency": payment.currency,
            }
        )

    allocations = (
        db.query(PaymentAllocation, Payment, Invoice, Subscriber)
        .join(Payment, PaymentAllocation.payment_id == Payment.id)
        .join(Invoice, PaymentAllocation.invoice_id == Invoice.id)
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .join(Reseller, Subscriber.reseller_id == Reseller.id)
        .filter(Payment.billing_account_id == ba.id)
        .filter(Payment.is_active.is_(True))
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Subscriber.reseller_id == ba.reseller_id)
        .order_by(PaymentAllocation.created_at.desc())
        .limit(limit)
        .all()
    )
    for allocation, payment, invoice, subscriber in allocations:
        subscriber_name = _activity_subscriber_name(subscriber)
        invoice_label = invoice.invoice_number or str(invoice.id)
        entries.append(
            {
                "direction": "debit",
                "title": "Funds allocated",
                "description": f"{subscriber_name} · Invoice {invoice_label}",
                "occurred_at": allocation.created_at,
                "reference": str(invoice.id),
                "amount": allocation.amount,
                "currency": payment.currency or invoice.currency,
            }
        )
    entries.sort(
        key=lambda entry: entry.get("occurred_at") or datetime.min, reverse=True
    )
    return entries[:limit]


__all__ = [
    "account_activity",
    "get_billing_account_summary",
    "get_payment_methods_page",
    "list_payment_methods",
    "payment_method_api_dict",
    "allocate_unallocated_to_subscriber",
    "remove_payment_method",
    "set_default_payment_method",
    "start_consolidated_payment",
    "verify_and_record_consolidated_payment",
]
