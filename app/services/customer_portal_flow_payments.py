"""Online payment provider flows for customer portal."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentMethodType,
    PaymentProvider,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services.billing._common import lock_account
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.collections import get_available_balance, restore_account_services
from app.services.common import round_money, to_decimal
from app.services.customer_portal_context import (
    get_allowed_account_ids,
    get_invoice_billing_contact,
)
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.settings_spec import resolve_value
from app.services.topup_intents import set_topup_intent_status

logger = logging.getLogger(__name__)
_TOPUP_INTENT_TTL = timedelta(minutes=30)
_ONLINE_PROVIDER_LABELS = {
    "paystack": "Pay with Paystack",
    "flutterwave": "Pay with Flutterwave",
}
_DIRECT_TRANSFER_PROVIDER = "direct_bank_transfer"
_DIRECT_TRANSFER_LABEL = "Direct bank transfer"
_DIRECT_TRANSFER_TTL = timedelta(days=7)


def _resolve_payment_provider(db: Session) -> str:
    """Return the configured payment provider type ('paystack' or 'flutterwave')."""
    val = resolve_value(db, SettingDomain.billing, "default_payment_provider_type")
    if val and str(val) == "flutterwave":
        return "flutterwave"
    return "paystack"


def _provider_uuid(db: Session, provider_type: str) -> uuid.UUID | None:
    """Resolve the PaymentProvider row id for a gateway type.

    Stamping provider_id on verify-path payments is what lets the webhook
    ingest path (and the (provider_id, external_id) unique index) recognise
    the same gateway transaction and refuse to credit it twice.
    """
    try:
        provider = billing_service.payment_providers.get_by_type(
            db, PaymentProviderType(provider_type)
        )
    except ValueError:
        return None
    return provider.id if provider else None


def _topup_payment_options(db: Session, default_provider: str) -> list[dict[str, str]]:
    """Return active online provider options for customer top-ups."""
    allowed_provider_types = {"paystack"}
    provider_types: list[str] = (
        [default_provider] if default_provider in allowed_provider_types else []
    )
    try:
        rows = db.scalars(
            select(PaymentProvider.provider_type)
            .where(PaymentProvider.is_active.is_(True))
            .where(PaymentProvider.provider_type.in_([PaymentProviderType.paystack]))
            .order_by(PaymentProvider.name)
        ).all()
        for provider_type in rows:
            value = getattr(provider_type, "value", str(provider_type))
            if value in allowed_provider_types and value not in provider_types:
                provider_types.append(value)
    except Exception:
        logger.debug("Failed to resolve active payment providers", exc_info=True)

    for provider_type in ("paystack",):
        if provider_type not in provider_types:
            provider_types.append(provider_type)

    options = [
        {
            "provider_type": provider_type,
            "label": _ONLINE_PROVIDER_LABELS[provider_type],
        }
        for provider_type in provider_types
        if provider_type in _ONLINE_PROVIDER_LABELS
    ]
    if direct_bank_transfer_enabled(db):
        options.append(
            {
                "provider_type": _DIRECT_TRANSFER_PROVIDER,
                "label": _DIRECT_TRANSFER_LABEL,
            }
        )
    return options


def direct_bank_transfer_settings(db: Session) -> dict[str, str]:
    """Customer-visible direct bank transfer settings."""
    keys = [
        "direct_bank_transfer_enabled",
        "direct_bank_transfer_bank_name",
        "direct_bank_transfer_account_name",
        "direct_bank_transfer_account_number",
        "direct_bank_transfer_instructions",
        "direct_bank_transfer_accounts",
    ]
    settings = dict.fromkeys(keys, "")
    rows = db.scalars(
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.billing)
        .where(DomainSetting.key.in_(keys))
        .where(DomainSetting.is_active.is_(True))
    ).all()
    for row in rows:
        settings[row.key] = str(row.value_text or "").strip()
    settings["direct_bank_transfer_accounts_list"] = direct_bank_transfer_accounts(
        settings
    )
    return settings


def direct_bank_transfer_accounts(
    settings: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    settings = settings or {}
    raw = settings.get("direct_bank_transfer_accounts") or ""
    accounts: list[dict[str, str]] = []
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                account = {
                    "id": str(item.get("id") or "").strip() or uuid.uuid4().hex,
                    "enabled": "true"
                    if str(item.get("enabled", "")).lower()
                    in {"1", "true", "yes", "on"}
                    else "false",
                    "bank_name": str(item.get("bank_name") or "").strip(),
                    "account_name": str(item.get("account_name") or "").strip(),
                    "account_number": str(item.get("account_number") or "").strip(),
                }
                if (
                    account["bank_name"]
                    and account["account_name"]
                    and account["account_number"]
                ):
                    accounts.append(account)
    if accounts:
        return accounts

    bank_name = (settings.get("direct_bank_transfer_bank_name") or "").strip()
    account_name = (settings.get("direct_bank_transfer_account_name") or "").strip()
    account_number = (settings.get("direct_bank_transfer_account_number") or "").strip()
    if bank_name and account_name and account_number:
        accounts.append(
            {
                "id": "legacy",
                "enabled": "true",
                "bank_name": bank_name,
                "account_name": account_name,
                "account_number": account_number,
            }
        )
    return accounts


def enabled_direct_bank_transfer_accounts(db: Session) -> list[dict[str, str]]:
    settings = direct_bank_transfer_settings(db)
    return [
        account
        for account in settings.get("direct_bank_transfer_accounts_list", [])
        if account.get("enabled") == "true"
    ]


def direct_bank_transfer_enabled(db: Session) -> bool:
    settings = direct_bank_transfer_settings(db)
    enabled = settings.get("direct_bank_transfer_enabled", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return bool(enabled and enabled_direct_bank_transfer_accounts(db))


def _resolve_topup_limits(db: Session) -> tuple[int, int]:
    """Return minimum and maximum allowed top-up amounts."""
    min_amount = resolve_value(db, SettingDomain.billing, "topup_min_amount")
    max_amount = resolve_value(db, SettingDomain.billing, "topup_max_amount")
    min_amount_value = (
        int(min_amount) if isinstance(min_amount, (str, int, float)) else 1000
    )
    max_amount_value = (
        int(max_amount) if isinstance(max_amount, (str, int, float)) else 500000
    )
    return min_amount_value, max_amount_value


def _format_naira(amount: Decimal | int | float) -> str:
    rounded = round_money(to_decimal(amount))
    return f"₦{rounded:,.2f}"


def _customer_account_uuid(customer: dict) -> uuid.UUID:
    raw_account_id = customer.get("account_id")
    if not raw_account_id:
        raise ValueError("Customer account is missing")
    return uuid.UUID(str(raw_account_id))


def _topup_policy_warnings(intent: TopupIntent) -> list[str]:
    metadata = dict(intent.metadata_ or {})
    violations = list(metadata.get("policy_violations") or [])
    requested_amount = round_money(to_decimal(metadata.get("requested_amount") or 0))
    actual_amount = round_money(
        to_decimal(metadata.get("actual_amount") or requested_amount or 0)
    )
    warnings: list[str] = []
    if "amount_mismatch" in violations:
        warnings.append(
            "The amount confirmed by the payment provider differed from the amount requested at checkout."
        )
    if "amount_below_min" in violations:
        warnings.append(
            f"The confirmed amount was below the usual minimum add-funds amount of {_format_naira(metadata.get('min_amount') or 0)}."
        )
    if "amount_above_max" in violations:
        warnings.append(
            f"The confirmed amount was above the usual maximum add-funds amount of {_format_naira(metadata.get('max_amount') or 0)}."
        )
    if "intent_expired" in violations:
        warnings.append(
            "The payment completed after the original checkout session had expired."
        )
    if warnings and requested_amount and actual_amount:
        warnings.insert(
            0,
            f"Requested {_format_naira(requested_amount)} but the provider confirmed {_format_naira(actual_amount)}.",
        )
    return warnings


def _build_topup_policy_violations(
    *,
    requested_amount: Decimal,
    actual_amount: Decimal,
    min_amount: int,
    max_amount: int,
    expires_at: datetime | None,
) -> list[str]:
    violations: list[str] = []
    if actual_amount != requested_amount:
        violations.append("amount_mismatch")
    if actual_amount < Decimal(str(min_amount)):
        violations.append("amount_below_min")
    if actual_amount > Decimal(str(max_amount)):
        violations.append("amount_above_max")
    normalized_expires_at = expires_at
    if normalized_expires_at and normalized_expires_at.tzinfo is None:
        normalized_expires_at = normalized_expires_at.replace(tzinfo=UTC)
    if normalized_expires_at and normalized_expires_at < datetime.now(UTC):
        violations.append("intent_expired")
    return violations


def _finalize_topup_intent(
    db: Session,
    intent: TopupIntent,
    *,
    payment: Payment,
    external_id: str,
    actual_amount: Decimal,
    policy_violations: list[str],
    min_amount: int,
    max_amount: int,
) -> None:
    metadata = dict(intent.metadata_ or {})
    metadata.update(
        {
            "requested_amount": str(intent.requested_amount),
            "actual_amount": str(actual_amount),
            "min_amount": min_amount,
            "max_amount": max_amount,
            "policy_violations": policy_violations,
        }
    )
    intent.completed_payment_id = payment.id
    intent.external_id = external_id
    intent.actual_amount = actual_amount
    set_topup_intent_status(intent, "completed", source="portal_verify")
    intent.completed_at = datetime.now(UTC)
    intent.metadata_ = metadata
    db.add(intent)
    db.commit()
    db.refresh(intent)


def _retry_topup_restore(db: Session, account_id: uuid.UUID) -> None:
    try:
        restore_account_services(db, str(account_id))
    except Exception as exc:
        logger.warning(
            "Best-effort service restore retry failed for account %s: %s",
            account_id,
            exc,
        )


def _build_topup_result(
    db: Session,
    *,
    payment: Payment,
    intent: TopupIntent,
    amount: Decimal,
    reference: str,
    already_recorded: bool,
) -> dict:
    return {
        "payment": payment,
        "amount": amount,
        "reference": reference,
        "already_recorded": already_recorded,
        "policy_warnings": _topup_policy_warnings(intent),
        **_build_topup_summary(db, payment),
    }


def _build_topup_summary(db: Session, payment: Payment) -> dict:
    """Describe how a top-up was allocated and what credit remains."""
    allocations = db.scalars(
        select(PaymentAllocation).where(
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.is_active.is_(True),
        )
    ).all()

    invoice_ids = [allocation.invoice_id for allocation in allocations]
    invoices_by_id: dict[str, Invoice] = {}
    if invoice_ids:
        invoices = db.scalars(select(Invoice).where(Invoice.id.in_(invoice_ids))).all()
        invoices_by_id = {str(invoice.id): invoice for invoice in invoices}

    allocated_to_invoices: list[dict[str, object]] = []
    total_allocated = Decimal("0.00")
    for allocation in allocations:
        amount = round_money(to_decimal(getattr(allocation, "amount", 0) or 0))
        total_allocated += amount
        invoice = invoices_by_id.get(str(allocation.invoice_id))
        allocated_to_invoices.append(
            {
                "invoice_id": str(allocation.invoice_id),
                "invoice_number": getattr(invoice, "invoice_number", None),
                "amount": amount,
            }
        )

    total_allocated = round_money(total_allocated)
    payment_amount = round_money(to_decimal(getattr(payment, "amount", 0) or 0))
    credit_added = round_money(max(Decimal("0.00"), payment_amount - total_allocated))

    available_balance: Decimal | None = None
    try:
        available_balance = round_money(
            get_available_balance(db, str(payment.account_id))
        )
    except Exception:
        logger.warning(
            "Failed to resolve available balance after top-up for account %s",
            payment.account_id,
            exc_info=True,
        )

    return {
        "allocated_to_invoices": allocated_to_invoices,
        "allocated_total": total_allocated,
        "credit_added": credit_added,
        "available_balance": available_balance,
    }


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

    if invoice.status in (
        InvoiceStatus.paid,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    ):
        return None

    provider_type = _resolve_payment_provider(db)
    invoice_number = getattr(invoice, "invoice_number", None)

    billing_contact = get_invoice_billing_contact(db, invoice, customer)
    email = billing_contact["billing_email"] or _resolve_customer_email(db, customer)

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
    amount_naira = round_money(tx.amount)

    if not invoice_id:
        raise ValueError("Payment metadata missing invoice_id")

    # Idempotency: check if a payment with this external reference already exists
    existing_payment = db.scalars(
        select(Payment).where(Payment.external_id == tx.external_id)
    ).first()
    if existing_payment:
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, existing_payment)
        return {
            "payment": existing_payment,
            "invoice": invoice,
            "amount": getattr(existing_payment, "amount", amount_naira),
            "reference": reference,
            "already_recorded": True,
            **summary,
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

    # Serialize concurrent verifies (double-click, refresh, verify racing the
    # webhook) for this account, then re-check under the lock.
    lock_account(db, str(invoice.account_id))
    existing_payment = db.scalars(
        select(Payment).where(Payment.external_id == tx.external_id)
    ).first()
    if existing_payment:
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, existing_payment)
        return {
            "payment": existing_payment,
            "invoice": invoice,
            "amount": getattr(existing_payment, "amount", amount_naira),
            "reference": reference,
            "already_recorded": True,
            **summary,
        }

    invoice_balance_due = round_money(
        to_decimal(getattr(invoice, "balance_due", amount_naira) or amount_naira)
    )
    if invoice_balance_due <= Decimal("0.00"):
        raise ValueError("Invoice no longer has an outstanding balance")
    allocated_amount = min(amount_naira, invoice_balance_due)
    try:
        payment = billing_adapter.record_payment(
            db,
            PaymentIntent(
                account_id=_UUID(str(invoice.account_id)),
                amount=amount_naira,
                currency=tx.currency,
                status=PaymentStatus.succeeded,
                provider_id=_provider_uuid(db, provider_type),
                external_id=tx.external_id,
                memo=f"{tx.memo_prefix} payment ref: {reference}",
                allocations=[
                    PaymentAllocationApply(
                        invoice_id=_UUID(str(invoice_id)),
                        amount=allocated_amount,
                    )
                ],
            ),
        )
    except IntegrityError:
        db.rollback()
        payment = db.scalars(
            select(Payment).where(Payment.external_id == tx.external_id)
        ).first()
        if payment is None:
            raise
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, payment)
        return {
            "payment": payment,
            "invoice": invoice,
            "amount": getattr(payment, "amount", amount_naira),
            "reference": reference,
            "already_recorded": True,
            **summary,
        }
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    summary = _build_topup_summary(db, payment)

    return {
        "payment": payment,
        "invoice": invoice,
        "amount": amount_naira,
        "reference": reference,
        "already_recorded": False,
        **summary,
    }


def _resolve_customer_email(db: Session, customer: dict) -> str:
    """Resolve a real email address for the customer (for payment gateways).

    The session ``username`` is the RADIUS/PPPoE login (or an impersonation
    token), not an email, so Paystack rejects it. Prefer an email already on the
    session, then fall back to the subscriber record. Returns "" if none.
    """
    for candidate in (customer.get("email"), customer.get("billing_email")):
        value = str(candidate or "").strip()
        if "@" in value:
            return value
    account_id = customer.get("account_id")
    if account_id:
        try:
            subscriber = db.get(Subscriber, uuid.UUID(str(account_id)))
        except (ValueError, TypeError):
            subscriber = None
        if subscriber:
            value = str(getattr(subscriber, "email", "") or "").strip()
            if "@" in value:
                return value
    return ""


def get_topup_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the customer top-up page."""
    account_id = customer.get("account_id")
    provider_type = _resolve_payment_provider(db)

    # Resolve current balance
    prepaid_balance: Decimal | None = None
    try:
        prepaid_balance = round_money(get_available_balance(db, str(account_id)))
    except Exception:
        logger.warning(
            "Failed to resolve prepaid balance for account %s",
            account_id,
            exc_info=True,
        )

    min_amount_value, max_amount_value = _resolve_topup_limits(db)

    email = _resolve_customer_email(db, customer)
    payment_methods = []
    if account_id:
        try:
            payment_methods = customer_cards.list_for_account(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve payment methods for account %s",
                account_id,
                exc_info=True,
            )

    context = {
        "provider_type": provider_type,
        "payment_options": _topup_payment_options(db, provider_type),
        "customer_email": email,
        "prepaid_balance": prepaid_balance,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "preset_amounts": [1000, 2000, 5000, 10000, 20000, 50000],
        "payment_methods": payment_methods,
    }
    try:
        account_uuid = _customer_account_uuid(customer)
        pending_direct = _latest_pending_direct_transfer_intent(db, account_uuid)
    except Exception:
        pending_direct = None
    if pending_direct:
        context["pending_direct_transfer"] = {
            "reference": pending_direct.reference,
            "amount": pending_direct.requested_amount,
            "currency": pending_direct.currency,
        }

    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
    )
    context["provider_public_key"] = gateway_context.public_key
    if gateway_context.provider_type == "paystack":
        context["paystack_public_key"] = gateway_context.public_key

    return context


def get_payment_methods_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the customer payment-methods management page.

    Surfaces saved cards (with their default flag), the prepaid balance, and the
    direct-bank-transfer details so transfer is a first-class, discoverable
    method rather than a radio buried inside the top-up flow. Autopay status is
    layered on by the route (mirrors the top-up page)."""
    account_id = customer.get("account_id")

    cards = []
    if account_id:
        try:
            cards = customer_cards.list_for_account(db, str(account_id))
        except Exception:
            logger.warning(
                "Failed to resolve payment methods for account %s",
                account_id,
                exc_info=True,
            )
    # Only card-type methods are managed here; bank accounts (if ever stored)
    # are a separate concept and shouldn't appear as "saved cards".
    saved_cards = [c for c in cards if c.method_type == PaymentMethodType.card]

    prepaid_balance: Decimal | None = None
    if account_id:
        try:
            prepaid_balance = round_money(get_available_balance(db, str(account_id)))
        except Exception:
            logger.warning(
                "Failed to resolve prepaid balance for account %s",
                account_id,
                exc_info=True,
            )

    min_amount_value, max_amount_value = _resolve_topup_limits(db)

    return {
        "saved_cards": saved_cards,
        "prepaid_balance": prepaid_balance,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "provider_type": _resolve_payment_provider(db),
        "direct_bank_transfer_enabled": direct_bank_transfer_enabled(db),
        "bank_transfer": direct_bank_transfer_settings(db),
    }


_TOPUP_CHARGE_IDEMPOTENCY_SCOPE = "topup_saved_card_charge"


def _topup_intent_replay(db: Session, ref_id: str | None) -> dict | None:
    """Return the prior saved-card top-up intent for a replayed idempotency key.

    The card was already charged on the original request, so the replay points
    the client straight at verification rather than charging again."""
    intent = db.get(TopupIntent, _coerce_uuid_or_none(ref_id)) if ref_id else None
    if intent is None:
        return None
    return {
        "intent_id": str(intent.id),
        "provider_type": intent.provider_type,
        "provider_public_key": None,
        "reference": intent.reference,
        "requested_amount": intent.requested_amount,
        "currency": intent.currency,
        "checkout_metadata": dict(intent.metadata_ or {}),
        "charged": True,
        "checkout_url": None,
        "replayed": True,
    }


def _coerce_uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def create_topup_intent(
    db: Session,
    customer: dict,
    amount: Decimal | int | float | str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Create a server-owned top-up intent for checkout.

    When ``payment_method_id`` selects a saved card the customer's card is
    charged server-side; passing ``idempotency_key`` makes that charge safe
    against double-submit (a replay returns the original intent rather than
    charging the card a second time)."""
    account_id = _customer_account_uuid(customer)
    requested_amount = round_money(to_decimal(amount))
    if requested_amount <= Decimal("0.00"):
        raise ValueError("Top-up amount must be greater than ₦0.00")

    min_amount_value, max_amount_value = _resolve_topup_limits(db)
    if requested_amount < Decimal(str(min_amount_value)):
        raise ValueError(
            f"Top-up amount must be at least {_format_naira(min_amount_value)}"
        )
    if requested_amount > Decimal(str(max_amount_value)):
        raise ValueError(
            f"Top-up amount must not exceed {_format_naira(max_amount_value)}"
        )

    provider_type = provider or _resolve_payment_provider(db)
    if provider_type == _DIRECT_TRANSFER_PROVIDER:
        return create_direct_transfer_topup_intent(db, customer, requested_amount)

    _cancel_pending_direct_transfer_intents(db, account_id)
    selected_payment_method_id = str(payment_method_id or "").strip() or None
    selected_payment_method = None
    selected_payment_token = None
    if selected_payment_method_id:
        if provider_type != "paystack":
            raise ValueError("Saved cards can only be used with Paystack")
        selected_payment_method = customer_cards._owned(
            db, str(account_id), selected_payment_method_id
        )
        if selected_payment_method is None:
            raise ValueError("Payment method not found")
        selected_payment_token = billing_service.payment_methods.get_decrypted_token(
            db, str(selected_payment_method.id)
        )
        if not selected_payment_token:
            raise ValueError("Payment method is not chargeable")
    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
    )

    # Saved-card charges hit the card server-side, so they need double-submit
    # protection. Gateway-redirect flows are already deduped by the unique
    # gateway reference and need no key. Reserve the key BEFORE charging so a
    # concurrent same-key request fails the unique constraint here.
    idem_key = (idempotency_key or "").strip() or None
    reservation: IdempotencyKey | None = None
    if idem_key and selected_payment_method is not None:
        prior = db.scalars(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _TOPUP_CHARGE_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == idem_key,
            )
        ).first()
        if prior is not None:
            if str(prior.account_id) != str(account_id):
                raise ValueError("Idempotency key already used")
            replayed = _topup_intent_replay(db, prior.ref_id)
            if replayed is not None:
                return replayed
            db.delete(prior)
            db.commit()
        reservation = IdempotencyKey(
            scope=_TOPUP_CHARGE_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=account_id,
            ref_id=None,
        )
        db.add(reservation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            prior = db.scalars(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == _TOPUP_CHARGE_IDEMPOTENCY_SCOPE,
                    IdempotencyKey.key == idem_key,
                )
            ).first()
            replayed = _topup_intent_replay(db, prior.ref_id) if prior else None
            if replayed is not None:
                return replayed
            raise ValueError("A payment with this key is already in progress.")

    intent_metadata = {"payment_flow": "account_topup"}
    if selected_payment_method_id:
        intent_metadata["payment_method_id"] = selected_payment_method_id

    intent = TopupIntent(
        account_id=account_id,
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        currency="NGN",
        requested_amount=requested_amount,
        status="pending",
        expires_at=datetime.now(UTC) + _TOPUP_INTENT_TTL,
        metadata_=intent_metadata,
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)

    checkout_metadata = {
        "payment_flow": "account_topup",
        "topup_intent_id": str(intent.id),
        "account_id": str(account_id),
        **(
            {"payment_method_id": selected_payment_method_id}
            if selected_payment_method_id
            else {}
        ),
    }
    charged = False
    if selected_payment_method is not None:
        from app.services import paystack

        try:
            paystack.charge_authorization(
                db,
                authorization_code=selected_payment_token,
                email=_resolve_customer_email(db, customer),
                amount_kobo=paystack.amount_to_kobo(requested_amount),
                reference=gateway_context.reference,
                metadata=checkout_metadata,
            )
        except Exception:
            # Release the key so the customer can retry with a different card.
            if reservation is not None:
                db.delete(reservation)
                db.commit()
            raise
        charged = True
        if reservation is not None:
            reservation.ref_id = str(intent.id)
            db.add(reservation)
            db.commit()

    checkout_url = None
    if gateway_context.provider_type == "flutterwave":
        from app.services import flutterwave

        callback_url = redirect_url or "/portal/billing/topup/verify"
        if callback_url.startswith("/"):
            # Flutterwave requires an absolute redirect_url; a relative path
            # breaks the hosted-checkout return leg (mobile hits this branch).
            from app.services.email import _get_app_url

            base_url = _get_app_url(db) or ""
            if base_url:
                callback_url = f"{base_url}{callback_url}"
        separator = "&" if "?" in callback_url else "?"
        try:
            checkout = flutterwave.initialize_transaction(
                db,
                email=_resolve_customer_email(db, customer),
                amount=requested_amount,
                reference=gateway_context.reference,
                redirect_url=(
                    f"{callback_url}{separator}reference={gateway_context.reference}"
                    "&provider=flutterwave"
                ),
                metadata=checkout_metadata,
            )
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("Flutterwave checkout initialization failed", exc_info=True)
            raise ValueError(
                "Unable to start Flutterwave checkout. Check Flutterwave configuration and try again."
            ) from exc
        checkout_url = checkout.get("link")
        if not checkout_url:
            logger.warning(
                "Flutterwave checkout initialization returned no link: %s",
                checkout,
            )
            raise ValueError("Flutterwave did not return a checkout link")

    return {
        "intent_id": str(intent.id),
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "requested_amount": requested_amount,
        "currency": intent.currency,
        "checkout_metadata": checkout_metadata,
        "charged": charged,
        "checkout_url": checkout_url,
    }


def _cancel_pending_direct_transfer_intents(db: Session, account_id: uuid.UUID) -> None:
    pending = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .where(TopupIntent.provider_type == _DIRECT_TRANSFER_PROVIDER)
        .where(TopupIntent.status == "pending")
    ).all()
    changed = False
    for intent in pending:
        set_topup_intent_status(intent, "canceled", source="portal_replace")
        metadata = dict(intent.metadata_ or {})
        metadata["canceled_reason"] = "replaced_by_new_topup"
        intent.metadata_ = metadata
        db.add(intent)
        changed = True
    if changed:
        db.flush()


def _latest_pending_direct_transfer_intent(
    db: Session, account_id: uuid.UUID
) -> TopupIntent | None:
    return db.scalars(
        select(TopupIntent)
        .where(TopupIntent.account_id == account_id)
        .where(TopupIntent.provider_type == _DIRECT_TRANSFER_PROVIDER)
        .where(TopupIntent.status == "pending")
        .order_by(TopupIntent.created_at.desc())
    ).first()


def create_direct_transfer_topup_intent(
    db: Session,
    customer: dict,
    amount: Decimal | int | float | str,
) -> dict:
    """Create or replace a pending direct-transfer top-up intent."""
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")

    account_id = _customer_account_uuid(customer)
    requested_amount = round_money(to_decimal(amount))
    if requested_amount <= Decimal("0.00"):
        raise ValueError("Top-up amount must be greater than ₦0.00")

    min_amount_value, max_amount_value = _resolve_topup_limits(db)
    if requested_amount < Decimal(str(min_amount_value)):
        raise ValueError(
            f"Top-up amount must be at least {_format_naira(min_amount_value)}"
        )
    if requested_amount > Decimal(str(max_amount_value)):
        raise ValueError(
            f"Top-up amount must not exceed {_format_naira(max_amount_value)}"
        )

    _cancel_pending_direct_transfer_intents(db, account_id)
    intent = TopupIntent(
        account_id=account_id,
        reference=f"TRF-{uuid.uuid4().hex[:12].upper()}",
        provider_type=_DIRECT_TRANSFER_PROVIDER,
        currency="NGN",
        requested_amount=requested_amount,
        status="pending",
        expires_at=datetime.now(UTC) + _DIRECT_TRANSFER_TTL,
        metadata_={"payment_flow": "account_topup", "payment_method": "bank_transfer"},
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)
    return {
        "intent_id": str(intent.id),
        "provider_type": _DIRECT_TRANSFER_PROVIDER,
        "reference": intent.reference,
        "requested_amount": requested_amount,
        "currency": intent.currency,
        "redirect_url": "/portal/billing/topup/transfer",
    }


def get_direct_transfer_topup_page(db: Session, customer: dict) -> dict:
    """Build context for the customer direct-transfer instruction page."""
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")
    account_id = _customer_account_uuid(customer)
    intent = _latest_pending_direct_transfer_intent(db, account_id)
    if not intent:
        raise ValueError("Start a direct bank transfer payment first")
    return {
        "intent": intent,
        "bank_transfer": direct_bank_transfer_settings(db),
        "bank_transfer_accounts": enabled_direct_bank_transfer_accounts(db),
    }


async def submit_direct_transfer_topup(
    db: Session,
    customer: dict,
    *,
    made_payment: bool,
    file: UploadFile,
    selected_account_id: str | None = None,
) -> dict:
    """Submit the pending direct-transfer top-up for admin review."""
    if not made_payment:
        raise ValueError("Confirm that you have made the payment")
    settings = direct_bank_transfer_settings(db)
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")

    account_id = _customer_account_uuid(customer)
    intent = _latest_pending_direct_transfer_intent(db, account_id)
    if not intent:
        raise ValueError("Start a direct bank transfer payment first")
    accounts = enabled_direct_bank_transfer_accounts(db)
    if not accounts:
        raise ValueError("Direct bank transfer is not configured")
    selected_account = accounts[0]
    if len(accounts) > 1:
        selected_account_id = str(selected_account_id or "").strip()
        selected_account = next(
            (
                account
                for account in accounts
                if str(account.get("id")) == selected_account_id
            ),
            None,
        )
        if not selected_account:
            raise ValueError("Choose the bank account you paid into")

    from app.services import payment_proofs

    path = await payment_proofs.save_proof_file(file)
    proof = payment_proofs.submit_proof(
        db,
        str(account_id),
        submitted_by=str(customer.get("subscriber_id") or account_id),
        amount=intent.requested_amount,
        bank_name=selected_account.get("bank_name"),
        reference=intent.reference,
        paid_at=datetime.now(UTC),
        file_path=path,
    )
    set_topup_intent_status(intent, "submitted", source="portal_proof_submit")
    metadata = dict(intent.metadata_ or {})
    metadata["payment_proof_id"] = proof.get("id")
    metadata["selected_bank_account"] = {
        "id": selected_account.get("id"),
        "bank_name": selected_account.get("bank_name"),
        "account_name": selected_account.get("account_name"),
        "account_number": selected_account.get("account_number"),
    }
    intent.metadata_ = metadata
    db.add(intent)
    db.commit()
    return proof


def verify_and_record_topup(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify a top-up payment and add credit to account balance."""
    account_id = _customer_account_uuid(customer)
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if not intent:
        raise ValueError("Payment reference was not issued for this add-funds flow")
    if intent.account_id != account_id:
        raise ValueError("Payment reference does not belong to this account")

    # Serialize concurrent verifies of the same reference (double-click,
    # web+mobile, verify racing the webhook), then re-read the intent under
    # the lock so a winner's completion is visible here.
    lock_account(db, str(account_id))
    db.refresh(intent)

    if intent.completed_payment_id:
        completed_payment = db.get(Payment, intent.completed_payment_id)
        if not completed_payment:
            raise ValueError("Recorded top-up payment could not be found")
        if completed_payment.account_id != intent.account_id:
            raise ValueError("Recorded top-up belongs to a different account")
        _retry_topup_restore(db, intent.account_id)
        return _build_topup_result(
            db,
            payment=completed_payment,
            intent=intent,
            amount=round_money(to_decimal(completed_payment.amount or 0)),
            reference=reference,
            already_recorded=True,
        )

    provider_type = intent.provider_type or provider or _resolve_payment_provider(db)

    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
    )
    amount_naira = round_money(tx.amount)
    external_id = tx.external_id
    metadata = dict(tx.metadata or {})
    metadata_intent_id = str(metadata.get("topup_intent_id") or "")
    if metadata_intent_id and metadata_intent_id != str(intent.id):
        raise ValueError("Verified payment did not match the original checkout session")

    min_amount_value, max_amount_value = _resolve_topup_limits(db)
    policy_violations = _build_topup_policy_violations(
        requested_amount=round_money(intent.requested_amount),
        actual_amount=amount_naira,
        min_amount=min_amount_value,
        max_amount=max_amount_value,
        expires_at=intent.expires_at,
    )

    # Idempotency check
    existing = db.scalars(
        select(Payment).where(Payment.external_id == external_id)
    ).first()
    if existing:
        if existing.account_id != intent.account_id:
            raise ValueError(
                "Payment reference is already linked to a different account"
            )
        _finalize_topup_intent(
            db,
            intent,
            payment=existing,
            external_id=external_id,
            actual_amount=amount_naira,
            policy_violations=policy_violations,
            min_amount=min_amount_value,
            max_amount=max_amount_value,
        )
        _retry_topup_restore(db, intent.account_id)
        return _build_topup_result(
            db,
            payment=existing,
            intent=intent,
            amount=round_money(to_decimal(existing.amount or amount_naira)),
            reference=reference,
            already_recorded=True,
        )

    # Create unallocated payment (credit to account balance)
    from uuid import UUID as _UUID

    # No explicit allocations — auto-allocation pays outstanding invoices
    # first, then remaining amount goes to account credit. This is
    # intentional: a subscriber who owes money should settle debts before
    # accumulating credit.
    try:
        payment = billing_adapter.record_payment(
            db,
            PaymentIntent(
                account_id=_UUID(str(intent.account_id)),
                amount=amount_naira,
                currency=tx.currency,
                status=PaymentStatus.succeeded,
                provider_id=_provider_uuid(db, provider_type),
                external_id=external_id,
                memo=f"{tx.memo_prefix} prepaid top-up ref: {reference}",
                allocations=[],  # No invoice allocation — goes to account credit
            ),
        )
    except IntegrityError:
        # The (provider_id, external_id) unique index caught a concurrent
        # writer recording the same gateway transaction.
        db.rollback()
        existing = db.scalars(
            select(Payment).where(Payment.external_id == external_id)
        ).first()
        if existing is None:
            raise
        if existing.account_id != intent.account_id:
            raise ValueError(
                "Payment reference is already linked to a different account"
            )
        _finalize_topup_intent(
            db,
            intent,
            payment=existing,
            external_id=external_id,
            actual_amount=amount_naira,
            policy_violations=policy_violations,
            min_amount=min_amount_value,
            max_amount=max_amount_value,
        )
        _retry_topup_restore(db, intent.account_id)
        return _build_topup_result(
            db,
            payment=existing,
            intent=intent,
            amount=round_money(to_decimal(existing.amount or amount_naira)),
            reference=reference,
            already_recorded=True,
        )
    _finalize_topup_intent(
        db,
        intent,
        payment=payment,
        external_id=external_id,
        actual_amount=amount_naira,
        policy_violations=policy_violations,
        min_amount=min_amount_value,
        max_amount=max_amount_value,
    )

    # Emit usage_topped_up event (triggers notification + potential service restore)
    from app.services.events import emit_event
    from app.services.events.types import EventType

    emit_event(
        db,
        EventType.usage_topped_up,
        {
            "account_id": str(intent.account_id),
            "amount": str(amount_naira),
            "reference": reference,
        },
        account_id=intent.account_id,
    )

    # Attempt to restore suspended prepaid subscriptions
    try:
        restored = restore_account_services(db, str(intent.account_id))
        if restored:
            logger.info(
                "Restored %d subscription(s) after prepaid top-up for account %s",
                restored,
                intent.account_id,
            )
    except Exception as exc:
        logger.warning(
            "Failed to auto-restore after top-up for account %s: %s",
            intent.account_id,
            exc,
        )

    return _build_topup_result(
        db,
        payment=payment,
        intent=intent,
        amount=amount_naira,
        reference=reference,
        already_recorded=False,
    )


__all__ = [
    "_resolve_payment_provider",
    "create_topup_intent",
    "create_direct_transfer_topup_intent",
    "direct_bank_transfer_enabled",
    "direct_bank_transfer_settings",
    "enabled_direct_bank_transfer_accounts",
    "get_direct_transfer_topup_page",
    "get_payment_page",
    "get_topup_page",
    "submit_direct_transfer_topup",
    "verify_and_record_payment",
    "verify_and_record_topup",
]
