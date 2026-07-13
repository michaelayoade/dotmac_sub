"""Online payment provider flows for customer portal."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import UploadFile
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentMethodType,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.services import billing as billing_service
from app.services import control_registry
from app.services import customer_portal_flow_payment_methods as customer_cards
from app.services.billing._common import lock_account
from app.services.billing_adapter import PaymentIntent, billing_adapter
from app.services.collections import get_available_balance, restore_account_services
from app.services.common import round_money, to_decimal
from app.services.customer_context import (
    customer_can_access_account,
    optional_customer_account_id,
    optional_customer_subscriber_id,
    require_customer_account_id,
)
from app.services.customer_portal_context import (
    get_invoice_billing_contact,
)
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.payment_routing import (
    eligible_routes,
    provider_for_intent,
    select_checkout_provider,
)
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
_DEFAULT_TOPUP_PRESET_AMOUNTS = (1000, 2000, 5000, 10000, 20000, 50000)


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


def _payment_by_gateway_identity(
    db: Session,
    *,
    external_id: str,
    provider_id: uuid.UUID | None,
) -> Payment | None:
    query = select(Payment).where(Payment.external_id == external_id)
    if provider_id is not None:
        query = query.where(
            or_(Payment.provider_id == provider_id, Payment.provider_id.is_(None))
        ).order_by((Payment.provider_id == provider_id).desc())
    return db.scalars(query).first()


def online_gateway_payment_options(
    db: Session,
    _legacy_default_provider: str | None = None,
) -> list[dict[str, str]]:
    """Return healthy gateways in canonical routing order."""
    return [
        {
            "provider_type": route.provider_type.value,
            "label": _ONLINE_PROVIDER_LABELS[route.provider_type.value],
        }
        for route in eligible_routes(db)
    ]


def _default_online_route(db: Session):
    try:
        return select_checkout_provider(db)
    except ValueError:
        return None


def _topup_payment_options(
    db: Session,
    _legacy_default_provider: str | None = None,
    *,
    direct_transfer_enabled: bool | None = None,
) -> list[dict[str, str]]:
    """Return online provider options for customer payments.

    Online gateways come from :func:`online_gateway_payment_options`; direct
    bank transfer is appended when configured.

    Pass ``direct_transfer_enabled`` when the caller has already resolved it to
    avoid re-running the bank-transfer settings query.
    """
    options = online_gateway_payment_options(db)
    if direct_transfer_enabled is None:
        direct_transfer_enabled = direct_bank_transfer_enabled(db)
    if direct_transfer_enabled:
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
        "direct_bank_transfer_sort_code",
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
                    "sort_code": str(item.get("sort_code") or "").strip(),
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
    sort_code = (settings.get("direct_bank_transfer_sort_code") or "").strip()
    if bank_name and account_name and account_number:
        accounts.append(
            {
                "id": "legacy",
                "enabled": "true",
                "bank_name": bank_name,
                "account_name": account_name,
                "account_number": account_number,
                "sort_code": sort_code,
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
    if not control_registry.is_enabled(db, "billing.direct_bank_transfer"):
        return False
    # Resolve from a single settings read (the accounts list is already attached
    # by direct_bank_transfer_settings) rather than calling
    # enabled_direct_bank_transfer_accounts, which would re-query the same rows.
    settings = direct_bank_transfer_settings(db)
    enabled = settings.get("direct_bank_transfer_enabled", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    has_account = any(
        account.get("enabled") == "true"
        for account in settings.get("direct_bank_transfer_accounts_list", [])
    )
    return bool(enabled and has_account)


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


def _default_topup_presets(min_amount: int, max_amount: int) -> list[int]:
    return [
        amount
        for amount in _DEFAULT_TOPUP_PRESET_AMOUNTS
        if min_amount <= amount <= max_amount
    ]


def _resolve_topup_presets(
    db: Session,
    *,
    min_amount: int,
    max_amount: int,
) -> list[int]:
    """Return configured top-up presets constrained by the active limits."""
    raw_presets = resolve_value(db, SettingDomain.billing, "topup_preset_amounts")
    if not isinstance(raw_presets, str) or not raw_presets.strip():
        return _default_topup_presets(min_amount, max_amount)

    presets: list[int] = []
    seen: set[int] = set()
    for part in raw_presets.split(","):
        try:
            amount = int(part.strip())
        except ValueError:
            return _default_topup_presets(min_amount, max_amount)
        if amount <= 0:
            return _default_topup_presets(min_amount, max_amount)
        if min_amount <= amount <= max_amount and amount not in seen:
            presets.append(amount)
            seen.add(amount)

    return presets or _default_topup_presets(min_amount, max_amount)


def _format_naira(amount: Decimal | int | float) -> str:
    rounded = round_money(to_decimal(amount))
    return f"₦{rounded:,.2f}"


def _customer_account_uuid(db: Session, customer: dict) -> uuid.UUID:
    account_id = require_customer_account_id(db, customer)
    return uuid.UUID(str(account_id))


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
        from app.services.billing.reconcile_unposted import (
            settle_prepaid_draft_invoices_from_credit,
        )

        settled = settle_prepaid_draft_invoices_from_credit(db, str(account_id))
        if settled.changed:
            logger.info(
                "Settled %d prepaid draft invoice(s) after top-up for account %s",
                len(settled.invoices_settled),
                account_id,
            )
            db.commit()
        restore_account_services(db, str(account_id))
    except Exception as exc:
        db.rollback()
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
        "provider_type": intent.provider_type,
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
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        return None

    if invoice.status in (
        InvoiceStatus.paid,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    ):
        return None

    billing_contact = get_invoice_billing_contact(db, invoice, customer)
    email = billing_contact["billing_email"] or _resolve_customer_email(db, customer)

    account_id = getattr(invoice, "account_id", None)
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

    amount_due = round_money(
        to_decimal(
            getattr(invoice, "balance_due", None) or getattr(invoice, "total", 0) or 0
        )
    )

    # The web chooser mints a per-provider reference/checkout via the intent
    # endpoint (mirroring the top-up flow). The bearer API
    # (``initiate_payment``) instead consumes a single pre-minted gateway
    # context, so keep ``provider_public_key``/``payment_reference`` for it.
    dbt_enabled = direct_bank_transfer_enabled(db)
    default_route = _default_online_route(db)
    gateway_context = None
    if default_route:
        gateway_context = payment_gateway_adapter.build_context(
            db,
            provider_type=default_route.provider_type.value,
            invoice_number=getattr(invoice, "invoice_number", None),
        )
    return {
        "invoice": invoice,
        "amount": amount_due,
        "provider_type": (
            gateway_context.provider_type
            if gateway_context
            else _DIRECT_TRANSFER_PROVIDER
            if dbt_enabled
            else None
        ),
        "provider_public_key": gateway_context.public_key if gateway_context else None,
        "paystack_public_key": gateway_context.public_key
        if gateway_context and gateway_context.provider_type == "paystack"
        else None,
        "payment_reference": gateway_context.reference if gateway_context else None,
        "payment_options": _topup_payment_options(
            db, direct_transfer_enabled=dbt_enabled
        ),
        "payment_methods": payment_methods,
        "direct_bank_transfer_enabled": dbt_enabled,
        "customer_email": email,
    }


_INVOICE_CHARGE_IDEMPOTENCY_SCOPE = "invoice_saved_card_charge"


def _invoice_charge_replay(reference: str) -> dict:
    """Return checkout context for a replayed saved-card invoice charge.

    The card was already charged on the original request, so the replay points
    the client straight at verification rather than charging again."""
    return {
        "provider_type": "paystack",
        "provider_public_key": None,
        "reference": reference,
        "charged": True,
        "checkout_url": None,
        "replayed": True,
    }


def _reserve_charge_idempotency_key(
    db: Session,
    *,
    scope: str,
    key: str,
    account_id: uuid.UUID,
    replay,
) -> tuple["IdempotencyKey | None", dict | None]:
    """Reserve an idempotency key before a server-side card charge.

    Returns ``(reservation, replay_result)``. If ``replay_result`` is not None
    the card was already charged on a prior request — the caller MUST return it
    immediately and not charge again. Otherwise ``reservation`` is a freshly
    committed row whose ``ref_id`` the caller sets after a successful charge
    (:func:`_commit_charge_idempotency_ref`) or releases on failure
    (:func:`_release_charge_idempotency_key`). ``replay(ref_id)`` maps a stored
    ref_id to a replay payload, or None when the prior attempt left no usable
    result (then the stale key is dropped and re-reserved).
    """
    prior = db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
    ).first()
    if prior is not None:
        if str(prior.account_id) != str(account_id):
            raise ValueError("Idempotency key already used")
        replayed = replay(prior.ref_id) if prior.ref_id else None
        if replayed is not None:
            return None, replayed
        db.delete(prior)
        db.commit()
    reservation = IdempotencyKey(
        scope=scope,
        key=key,
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
                IdempotencyKey.scope == scope,
                IdempotencyKey.key == key,
            )
        ).first()
        replayed = replay(prior.ref_id) if (prior and prior.ref_id) else None
        if replayed is not None:
            return None, replayed
        raise ValueError("A payment with this key is already in progress.")
    return reservation, None


def _release_charge_idempotency_key(
    db: Session, reservation: "IdempotencyKey | None"
) -> None:
    """Release a reserved key so the customer can retry (e.g. after a decline)."""
    if reservation is not None:
        db.delete(reservation)
        db.commit()


def _commit_charge_idempotency_ref(
    db: Session, reservation: "IdempotencyKey | None", ref_id: str
) -> None:
    """Bind a successful charge's ref to its reserved key so replays are safe."""
    if reservation is not None:
        reservation.ref_id = ref_id
        db.add(reservation)
        db.commit()


def _init_flutterwave_checkout(
    db: Session,
    customer: dict,
    *,
    amount: Decimal,
    reference: str,
    redirect_url: str | None,
    metadata: dict,
    default_callback_path: str,
    currency: str | None = None,
) -> str:
    """Start a Flutterwave hosted checkout and return its link.

    Shared by the top-up and invoice-pay flows; they differ only in
    ``default_callback_path`` (the verify route to return to).
    """
    from app.services import flutterwave

    callback_url = redirect_url or default_callback_path
    if callback_url.startswith("/"):
        # Flutterwave requires an absolute redirect_url; a relative path breaks
        # the hosted-checkout return leg (mobile hits this branch).
        from app.services.email import _get_app_url

        base_url = _get_app_url(db) or ""
        if base_url:
            callback_url = f"{base_url}{callback_url}"
    separator = "&" if "?" in callback_url else "?"
    try:
        checkout = flutterwave.initialize_transaction(
            db,
            email=_resolve_customer_email(db, customer),
            amount=amount,
            reference=reference,
            redirect_url=(
                f"{callback_url}{separator}reference={reference}&provider=flutterwave"
            ),
            metadata=metadata,
            currency=currency,
        )
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("Flutterwave checkout initialization failed", exc_info=True)
        raise ValueError(
            "Unable to start Flutterwave checkout. Check Flutterwave configuration and try again."
        ) from exc
    link = checkout.get("link")
    if not link:
        logger.warning(
            "Flutterwave checkout initialization returned no link: %s", checkout
        )
        raise ValueError("Flutterwave did not return a checkout link")
    return link


def _charge_saved_card_for_invoice(
    db: Session,
    customer: dict,
    *,
    invoice: Invoice,
    amount: Decimal,
    payment_method_id: str,
    checkout_metadata: dict,
    provider_id: str,
    idempotency_key: str | None,
) -> dict:
    """Charge a saved card server-side for an invoice (Paystack only).

    Recording/allocation happens in :func:`verify_and_record_payment` when the
    client returns to the verify route — the gateway metadata carries the
    ``invoice_id``. An ``idempotency_key`` makes the charge safe against
    double-submit: a replay returns the original reference rather than charging
    the card a second time."""
    account_id = uuid.UUID(str(invoice.account_id))
    method = customer_cards._owned(db, str(account_id), payment_method_id)
    if method is None:
        raise ValueError("Payment method not found")
    token = billing_service.payment_methods.get_decrypted_token(db, str(method.id))
    if not token:
        raise ValueError("Payment method is not chargeable")

    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type="paystack",
        invoice_number=getattr(invoice, "invoice_number", None),
    )
    reference = gateway_context.reference

    # Reserve the idempotency key BEFORE charging so a concurrent (or replayed)
    # same-key request returns the original reference instead of charging twice.
    idem_key = (idempotency_key or "").strip() or None
    reservation: IdempotencyKey | None = None
    if idem_key:
        reservation, replayed = _reserve_charge_idempotency_key(
            db,
            scope=_INVOICE_CHARGE_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=account_id,
            replay=lambda ref_id: _invoice_charge_replay(ref_id) if ref_id else None,
        )
        if replayed is not None:
            return replayed

    intent = _record_invoice_checkout_intent(
        db,
        account_id=account_id,
        reference=reference,
        provider_type=gateway_context.provider_type,
        amount=amount,
        metadata={**checkout_metadata, "provider_id": provider_id},
    )

    from app.services import paystack

    try:
        paystack.charge_authorization(
            db,
            authorization_code=token,
            email=_resolve_customer_email(db, customer),
            amount_kobo=paystack.amount_to_kobo(amount),
            reference=reference,
            metadata=checkout_metadata,
        )
    except Exception:
        # Release the key so the customer can retry with a different card.
        _release_charge_idempotency_key(db, reservation)
        set_topup_intent_status(intent, "failed", source="saved_card_charge")
        db.add(intent)
        db.commit()
        raise
    _commit_charge_idempotency_ref(db, reservation, reference)

    return {
        "provider_type": "paystack",
        "provider_public_key": gateway_context.public_key,
        "reference": reference,
        "amount": amount,
        "currency": "NGN",
        "checkout_metadata": checkout_metadata,
        "charged": True,
        "checkout_url": None,
    }


def create_invoice_payment_intent(
    db: Session,
    customer: dict,
    invoice_id: str,
    *,
    provider: str | None = None,
    payment_method_id: str | None = None,
    redirect_url: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Start an invoice payment via the customer's chosen method.

    Mirrors :func:`create_topup_intent` but settles a specific invoice:

    * a **saved card** is charged server-side (Paystack only);
    * a **gateway** choice (Paystack inline / Flutterwave hosted) returns
      checkout context for the client to open;
    * a **bank transfer** hands off to the direct-transfer proof flow.

    The amount is the invoice balance (server-authoritative — the client cannot
    set it). The verified payment is allocated to ``invoice_id`` by
    :func:`verify_and_record_payment`, which reads ``invoice_id`` back from the
    gateway metadata.
    """
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        raise ValueError("Invoice not found or access denied")
    if invoice.status in (
        InvoiceStatus.paid,
        InvoiceStatus.void,
        InvoiceStatus.written_off,
    ):
        raise ValueError("Invoice is no longer payable")

    amount = round_money(
        to_decimal(
            getattr(invoice, "balance_due", None) or getattr(invoice, "total", 0) or 0
        )
    )
    if amount <= Decimal("0.00"):
        raise ValueError("Invoice no longer has an outstanding balance")

    # Bank transfer: reuse the direct-transfer proof flow, prefilled with the
    # invoice balance and tagged with the invoice so the proof is traceable.
    # Limits are NOT enforced here (a real invoice may be below the top-up
    # minimum, e.g. a small reconnection fee). The reviewed transfer credits the
    # account and auto-allocation settles outstanding invoices oldest-first.
    if provider == _DIRECT_TRANSFER_PROVIDER:
        return create_direct_transfer_topup_intent(
            db,
            customer,
            amount,
            invoice_id=str(invoice.id),
            enforce_limits=False,
        )

    route = select_checkout_provider(db, provider)
    provider_type = route.provider_type.value
    customer_email = _resolve_customer_email(db, customer)
    _require_gateway_email(provider_type, customer_email)

    invoice_number = getattr(invoice, "invoice_number", None)
    checkout_metadata = {
        "payment_flow": "invoice_payment",
        "invoice_id": str(invoice.id),
        "invoice_number": invoice_number or "",
        "account_id": str(invoice.account_id),
        "provider_id": route.provider_id,
    }

    # Saved card -> server-to-server Paystack charge.
    selected_payment_method_id = str(payment_method_id or "").strip() or None
    if selected_payment_method_id:
        if provider_type != "paystack":
            raise ValueError("Saved cards can only be used with Paystack")
        return _charge_saved_card_for_invoice(
            db,
            customer,
            invoice=invoice,
            amount=amount,
            payment_method_id=selected_payment_method_id,
            checkout_metadata=checkout_metadata,
            provider_id=route.provider_id,
            idempotency_key=idempotency_key,
        )

    # Gateway checkout (Paystack inline opened client-side, or Flutterwave
    # hosted checkout we initialize here).
    gateway_context = payment_gateway_adapter.build_context(
        db,
        provider_type=provider_type,
        invoice_number=invoice_number,
    )
    # Durable, expirable trace of the started checkout, mirroring the TopupIntent
    # the top-up flow always creates — so a hosted checkout that debits the
    # customer but never returns is reconcilable. Completed in
    # verify_and_record_payment's caller via complete_invoice_payment_intent.
    _record_invoice_checkout_intent(
        db,
        account_id=uuid.UUID(str(invoice.account_id)),
        reference=gateway_context.reference,
        provider_type=gateway_context.provider_type,
        amount=amount,
        metadata=checkout_metadata,
    )
    result = {
        "provider_type": gateway_context.provider_type,
        "provider_public_key": gateway_context.public_key,
        "reference": gateway_context.reference,
        "amount": amount,
        "currency": "NGN",
        "invoice_number": invoice_number,
        "customer_email": customer_email,
        "checkout_metadata": checkout_metadata,
        "charged": False,
        "checkout_url": None,
    }
    if gateway_context.provider_type == "flutterwave":
        result["checkout_url"] = _init_flutterwave_checkout(
            db,
            customer,
            amount=amount,
            reference=gateway_context.reference,
            redirect_url=redirect_url,
            metadata=checkout_metadata,
            default_callback_path="/portal/billing/pay/verify",
            currency=getattr(invoice, "currency", None),
        )
    return result


def _record_invoice_checkout_intent(
    db: Session,
    *,
    account_id: uuid.UUID,
    reference: str,
    provider_type: str,
    amount: Decimal,
    metadata: dict,
) -> TopupIntent:
    """Persist a pending record tracing a started invoice gateway checkout.

    Reuses ``TopupIntent`` so checkout selection is durable and verification
    cannot be redirected to a different provider by caller input.
    """
    intent = TopupIntent(
        account_id=account_id,
        reference=reference,
        provider_type=provider_type,
        currency="NGN",
        requested_amount=amount,
        status="pending",
        expires_at=datetime.now(UTC) + _TOPUP_INTENT_TTL,
        metadata_=dict(metadata),
    )
    db.add(intent)
    db.commit()
    db.refresh(intent)
    return intent


def complete_invoice_payment_intent(
    db: Session, reference: str, payment: Payment | None
) -> None:
    """Best-effort: mark a pending invoice checkout record completed after verify.

    No-op when the reference has no invoice checkout record (saved-card charges
    use an IdempotencyKey instead; bearer-API/legacy references have none)."""
    try:
        intent = db.scalars(
            select(TopupIntent).where(TopupIntent.reference == reference)
        ).first()
        if intent is None or intent.completed_payment_id:
            return
        if str((intent.metadata_ or {}).get("payment_flow")) != "invoice_payment":
            return
        intent.completed_payment_id = getattr(payment, "id", None)
        set_topup_intent_status(intent, "completed", source="invoice_verify")
        intent.completed_at = datetime.now(UTC)
        db.add(intent)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "Failed to complete invoice checkout intent for %s",
            reference,
            exc_info=True,
        )


def verify_and_record_payment(
    db: Session,
    customer: dict,
    reference: str,
    *,
    provider: str | None = None,
) -> dict:
    """Verify an online payment transaction and record the payment."""
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if (
        intent is None
        or str((intent.metadata_ or {}).get("payment_flow")) != "invoice_payment"
    ):
        raise ValueError("Payment reference was not issued for this invoice checkout")
    if not customer_can_access_account(db, customer, intent.account_id):
        raise ValueError("Payment reference does not belong to this account")
    provider_type = provider_for_intent(intent, provider).value

    tx = payment_gateway_adapter.verify(
        db,
        provider_type=provider_type,
        reference=reference,
    )
    invoice_id = tx.metadata.get("invoice_id")
    expected_invoice_id = str((intent.metadata_ or {}).get("invoice_id") or "")
    amount_naira = round_money(tx.amount)

    if not invoice_id:
        raise ValueError("Payment metadata missing invoice_id")
    if expected_invoice_id and str(invoice_id) != expected_invoice_id:
        raise ValueError("Verified payment did not match the original invoice checkout")

    provider_id = _coerce_uuid_or_none(
        (intent.metadata_ or {}).get("provider_id")
    ) or _provider_uuid(db, provider_type)

    # Idempotency: check if a payment with this external reference already exists
    existing_payment = _payment_by_gateway_identity(
        db, external_id=tx.external_id, provider_id=provider_id
    )
    if existing_payment:
        if existing_payment.account_id != intent.account_id:
            raise ValueError("Payment reference is linked to a different account")
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, existing_payment)
        complete_invoice_payment_intent(db, reference, existing_payment)
        return {
            "payment": existing_payment,
            "invoice": invoice,
            "amount": getattr(existing_payment, "amount", amount_naira),
            "reference": reference,
            "provider_type": provider_type,
            "already_recorded": True,
            **summary,
        }

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or not customer_can_access_account(
        db, customer, getattr(invoice, "account_id", None)
    ):
        raise ValueError("Invoice not found or access denied")

    from uuid import UUID as _UUID

    from app.schemas.billing import PaymentAllocationApply

    # Serialize concurrent verifies (double-click, refresh, verify racing the
    # webhook) for this account, then re-check under the lock.
    lock_account(db, str(invoice.account_id))
    existing_payment = _payment_by_gateway_identity(
        db, external_id=tx.external_id, provider_id=provider_id
    )
    if existing_payment:
        if existing_payment.account_id != intent.account_id:
            raise ValueError("Payment reference is linked to a different account")
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, existing_payment)
        complete_invoice_payment_intent(db, reference, existing_payment)
        return {
            "payment": existing_payment,
            "invoice": invoice,
            "amount": getattr(existing_payment, "amount", amount_naira),
            "reference": reference,
            "provider_type": provider_type,
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
                provider_id=provider_id,
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
        payment = _payment_by_gateway_identity(
            db, external_id=tx.external_id, provider_id=provider_id
        )
        if payment is None:
            raise
        if payment.account_id != intent.account_id:
            raise ValueError("Payment reference is linked to a different account")
        invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
        summary = _build_topup_summary(db, payment)
        complete_invoice_payment_intent(db, reference, payment)
        return {
            "payment": payment,
            "invoice": invoice,
            "amount": getattr(payment, "amount", amount_naira),
            "reference": reference,
            "provider_type": provider_type,
            "already_recorded": True,
            **summary,
        }
    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    summary = _build_topup_summary(db, payment)
    complete_invoice_payment_intent(db, reference, payment)

    return {
        "payment": payment,
        "invoice": invoice,
        "amount": amount_naira,
        "reference": reference,
        "provider_type": provider_type,
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
    account_id = optional_customer_account_id(db, customer)
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


def _require_gateway_email(provider_type: str, email: str) -> None:
    if provider_type != _DIRECT_TRANSFER_PROVIDER and not email:
        raise ValueError(
            "A valid customer email address is required before starting card payment."
        )


def get_topup_page(
    db: Session,
    customer: dict,
) -> dict:
    """Build context for the customer top-up page."""
    account_id = optional_customer_account_id(db, customer)
    default_route = _default_online_route(db)
    provider_type = (
        default_route.provider_type.value
        if default_route
        else _DIRECT_TRANSFER_PROVIDER
        if direct_bank_transfer_enabled(db)
        else None
    )

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
        "payment_options": _topup_payment_options(db),
        "customer_email": email,
        "prepaid_balance": prepaid_balance,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "preset_amounts": _resolve_topup_presets(
            db,
            min_amount=min_amount_value,
            max_amount=max_amount_value,
        ),
        "payment_methods": payment_methods,
    }
    try:
        account_uuid = _customer_account_uuid(db, customer)
        pending_direct = _latest_pending_direct_transfer_intent(db, account_uuid)
    except Exception:
        pending_direct = None
    if pending_direct:
        context["pending_direct_transfer"] = {
            "reference": pending_direct.reference,
            "amount": pending_direct.requested_amount,
            "currency": pending_direct.currency,
        }

    if default_route:
        gateway_context = payment_gateway_adapter.build_context(
            db,
            provider_type=default_route.provider_type.value,
        )
        context["provider_public_key"] = gateway_context.public_key
        if gateway_context.provider_type == "paystack":
            context["paystack_public_key"] = gateway_context.public_key
    else:
        context["provider_public_key"] = None

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
    account_id = optional_customer_account_id(db, customer)

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
    default_route = _default_online_route(db)

    return {
        "saved_cards": saved_cards,
        "prepaid_balance": prepaid_balance,
        "min_amount": min_amount_value,
        "max_amount": max_amount_value,
        "provider_type": (
            default_route.provider_type.value
            if default_route
            else _DIRECT_TRANSFER_PROVIDER
        ),
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
    account_id = _customer_account_uuid(db, customer)
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

    if provider == _DIRECT_TRANSFER_PROVIDER:
        return create_direct_transfer_topup_intent(db, customer, requested_amount)

    route = select_checkout_provider(db, provider)
    provider_type = route.provider_type.value

    customer_email = _resolve_customer_email(db, customer)
    _require_gateway_email(provider_type, customer_email)

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
        reservation, replayed = _reserve_charge_idempotency_key(
            db,
            scope=_TOPUP_CHARGE_IDEMPOTENCY_SCOPE,
            key=idem_key,
            account_id=account_id,
            replay=lambda ref_id: _topup_intent_replay(db, ref_id),
        )
        if replayed is not None:
            return replayed

    intent_metadata = {
        "payment_flow": "account_topup",
        "provider_id": route.provider_id,
    }
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
                email=customer_email,
                amount_kobo=paystack.amount_to_kobo(requested_amount),
                reference=gateway_context.reference,
                metadata=checkout_metadata,
            )
        except Exception:
            # Release the key so the customer can retry with a different card.
            _release_charge_idempotency_key(db, reservation)
            raise
        charged = True
        _commit_charge_idempotency_ref(db, reservation, str(intent.id))

    checkout_url = None
    if gateway_context.provider_type == "flutterwave":
        checkout_url = _init_flutterwave_checkout(
            db,
            customer,
            amount=requested_amount,
            reference=gateway_context.reference,
            redirect_url=redirect_url,
            metadata=checkout_metadata,
            default_callback_path="/portal/billing/topup/verify",
        )

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
    *,
    invoice_id: str | None = None,
    enforce_limits: bool = True,
) -> dict:
    """Create or replace a pending direct-transfer intent.

    Used for both account top-ups and (with ``invoice_id`` set) invoice
    payments. For invoice payments ``enforce_limits=False`` skips the top-up
    min/max so a small invoice can still be paid by transfer, and the invoice is
    tagged in metadata so the submitted proof is traceable to it.
    """
    if not direct_bank_transfer_enabled(db):
        raise ValueError("Direct bank transfer is not configured")

    account_id = _customer_account_uuid(db, customer)
    requested_amount = round_money(to_decimal(amount))
    if requested_amount <= Decimal("0.00"):
        raise ValueError("Top-up amount must be greater than ₦0.00")

    if enforce_limits:
        min_amount_value, max_amount_value = _resolve_topup_limits(db)
        if requested_amount < Decimal(str(min_amount_value)):
            raise ValueError(
                f"Top-up amount must be at least {_format_naira(min_amount_value)}"
            )
        if requested_amount > Decimal(str(max_amount_value)):
            raise ValueError(
                f"Top-up amount must not exceed {_format_naira(max_amount_value)}"
            )

    intent_metadata: dict[str, str] = {
        "payment_method": "bank_transfer",
        "payment_flow": "invoice_payment" if invoice_id else "account_topup",
    }
    if invoice_id:
        intent_metadata["invoice_id"] = invoice_id

    _cancel_pending_direct_transfer_intents(db, account_id)
    intent = TopupIntent(
        account_id=account_id,
        reference=f"TRF-{uuid.uuid4().hex[:12].upper()}",
        provider_type=_DIRECT_TRANSFER_PROVIDER,
        currency="NGN",
        requested_amount=requested_amount,
        status="pending",
        expires_at=datetime.now(UTC) + _DIRECT_TRANSFER_TTL,
        metadata_=intent_metadata,
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
    account_id = _customer_account_uuid(db, customer)
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

    account_id = _customer_account_uuid(db, customer)
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
        submitted_by=str(optional_customer_subscriber_id(db, customer) or account_id),
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
    account_id = _customer_account_uuid(db, customer)
    intent = db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()
    if not intent:
        raise ValueError("Payment reference was not issued for this add-funds flow")
    if intent.account_id != account_id:
        raise ValueError("Payment reference does not belong to this account")
    provider_type = provider_for_intent(intent, provider).value
    provider_id = _coerce_uuid_or_none(
        (intent.metadata_ or {}).get("provider_id")
    ) or _provider_uuid(db, provider_type)

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
    existing = _payment_by_gateway_identity(
        db, external_id=external_id, provider_id=provider_id
    )
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
                provider_id=provider_id,
                external_id=external_id,
                memo=f"{tx.memo_prefix} prepaid top-up ref: {reference}",
                allocations=[],  # No invoice allocation — goes to account credit
            ),
        )
    except IntegrityError:
        # The (provider_id, external_id) unique index caught a concurrent
        # writer recording the same gateway transaction.
        db.rollback()
        existing = _payment_by_gateway_identity(
            db, external_id=external_id, provider_id=provider_id
        )
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
        from app.services.billing.reconcile_unposted import (
            settle_prepaid_draft_invoices_from_credit,
        )

        settled = settle_prepaid_draft_invoices_from_credit(db, str(intent.account_id))
        if settled.changed:
            logger.info(
                "Settled %d prepaid draft invoice(s) after top-up for account %s",
                len(settled.invoices_settled),
                intent.account_id,
            )
            db.commit()
        restored = restore_account_services(db, str(intent.account_id))
        if restored:
            logger.info(
                "Restored %d subscription(s) after prepaid top-up for account %s",
                restored,
                intent.account_id,
            )
    except Exception as exc:
        db.rollback()
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
    "complete_invoice_payment_intent",
    "create_invoice_payment_intent",
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
