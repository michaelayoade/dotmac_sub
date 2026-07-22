"""Plan change and change-request flows for customer portal."""

import logging
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, PriceType, Subscription
from app.models.subscriber import Address, Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services import catalog as catalog_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_context import (
    optional_customer_account_id,
    optional_customer_subscriber_id,
)
from app.services.customer_financial_position import (
    CustomerFinancialPosition,
    get_customer_financial_position,
)
from app.services.customer_portal_context import (
    get_available_portal_offers,
    offer_has_positive_recurring_price,
)
from app.services.customer_portal_flow_common import (
    _compute_total_pages,
    _resolve_next_billing_date,
)
from app.services.form_contracts import (
    FormConsequence,
    FormContract,
    FormPrerequisite,
)
from app.services.form_contracts import (
    register as register_form_contract,
)

logger = logging.getLogger(__name__)

_CYCLE_LABELS = {
    "daily": "/day",
    "weekly": "/week",
    "monthly": "/month",
    "quarterly": "/quarter",
    "annual": "/year",
}


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float | Decimal):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            return default
    return default


def _customer_credit_balance(db: Session, account_id: str | None) -> Decimal:
    if not account_id:
        return Decimal("0.00")
    return get_customer_financial_position(db, account_id).prepaid_available_balance


def _customer_financial_position(
    db: Session,
    account_id: str | None,
) -> CustomerFinancialPosition | None:
    if not account_id:
        return None
    return get_customer_financial_position(db, account_id)


def _collection_blocking_balance(
    db: Session,
    account_id: str | None,
) -> Decimal:
    position = _customer_financial_position(db, account_id)
    return (
        position.collection_blocking_balance
        if position is not None
        else Decimal("0.00")
    )


def _build_plan_change_quote(
    db: Session,
    subscription: Subscription,
    target_offer: CatalogOffer,
    *,
    prepaid_funding_before: Decimal | None = None,
    effective_at: datetime | None = None,
    target_service_address_id: str | None = None,
) -> dict[str, object]:
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionLifecycleCommand,
        preview_subscription_command,
    )

    preview = preview_subscription_command(
        db,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.change_plan,
            source="customer_portal:plan_change_quote",
            target_offer_id=str(target_offer.id),
            target_service_address_id=target_service_address_id,
            effective_at=effective_at,
        ),
        current_balance=prepaid_funding_before,
    )
    details = preview.billing_impact.details or {}
    quote = details.get("quote")
    if not isinstance(quote, dict):
        return {}
    field_quote = (
        preview.field_delivery_quote.as_dict()
        if preview.field_delivery_quote is not None
        else None
    )
    return {
        **quote,
        "delivery_mode": (
            preview.delivery_mode.value if preview.delivery_mode is not None else None
        ),
        "field_delivery_quote": field_quote,
    }


def _serialize_plan_change_quote(quote: dict[str, object]) -> dict[str, object]:
    raw_field_quote = quote.get("field_delivery_quote")
    field_quote = (
        {
            **raw_field_quote,
            "fee_amount": _to_float(raw_field_quote.get("fee_amount")),
        }
        if isinstance(raw_field_quote, dict)
        else None
    )
    return {
        "current_remaining_value": _to_float(
            quote.get("current_remaining_value", Decimal("0.00"))
        ),
        "required_amount": _to_float(quote.get("required_amount", Decimal("0.00"))),
        "prepaid_funding_before": _to_float(
            quote.get("prepaid_funding_before", Decimal("0.00"))
        ),
        "prepaid_funding_after": _to_float(
            quote.get("prepaid_funding_after", Decimal("0.00"))
        ),
        "postpaid_receivables": _to_float(
            quote.get("postpaid_receivables", Decimal("0.00"))
        ),
        "currency": str(quote.get("currency") or "NGN"),
        "preview_effective_at": str(quote.get("preview_effective_at") or ""),
        "shortfall": _to_float(quote.get("shortfall", Decimal("0.00"))),
        "collection_blocking_balance": _to_float(
            quote.get("collection_blocking_balance", Decimal("0.00"))
        ),
        "charge_amount": _to_float(quote.get("charge_amount", Decimal("0.00"))),
        "net_amount": _to_float(quote.get("net_amount", Decimal("0.00"))),
        "days_remaining": _to_int(quote.get("days_remaining", 0) or 0),
        "days_in_cycle": _to_int(quote.get("days_in_cycle", 0) or 0),
        "remaining_cycle_seconds": _to_int(
            quote.get("remaining_cycle_seconds", 0) or 0
        ),
        "total_cycle_seconds": _to_int(quote.get("total_cycle_seconds", 0) or 0),
        "can_apply_immediately": bool(quote.get("can_apply_immediately", False))
        and not (field_quote is not None and not field_quote.get("eligible", False)),
        "is_upgrade": bool(quote.get("is_upgrade", False)),
        "is_downgrade": bool(quote.get("is_downgrade", False)),
        "reason": quote.get("reason"),
        "preview_fingerprint": str(quote.get("preview_fingerprint") or ""),
        "has_financial_effect": bool(quote.get("ledger_entry_type")),
        "ledger_entry_type": quote.get("ledger_entry_type"),
        "ledger_source": quote.get("ledger_source"),
        "ledger_amount": _to_float(quote.get("ledger_amount", Decimal("0.00"))),
        "access_consequence": str(
            quote.get("access_consequence") or "none_plan_change_only"
        ),
        "delivery_mode": str(quote.get("delivery_mode") or "unknown"),
        "field_delivery_quote": field_quote,
    }


def _service_address_options(db: Session, subscription: Subscription) -> list[dict]:
    rows = (
        db.query(Address)
        .filter(Address.subscriber_id == subscription.subscriber_id)
        .order_by(Address.is_primary.desc(), Address.created_at.asc())
        .all()
    )
    return [
        {
            "id": str(address.id),
            "label": ", ".join(
                part
                for part in (
                    address.label,
                    address.address_line1,
                    address.city,
                    address.region,
                )
                if part
            ),
            "has_coordinates": (
                address.latitude is not None and address.longitude is not None
            ),
            "is_current": str(address.id) == str(subscription.service_address_id),
        }
        for address in rows
    ]


def _offer_delivery_modes(
    current_offer: CatalogOffer | None,
    offers: list[CatalogOffer],
) -> dict[str, str]:
    if current_offer is None:
        return {}
    from app.services.subscription_lifecycle import classify_service_change_delivery

    return {
        str(offer.id): classify_service_change_delivery(current_offer, offer).value
        for offer in offers
    }


# Editor contract for the customer plan-change form (ui.form_contracts pilot).
# The lifecycle command owner re-checks every prerequisite at execution time;
# this rendered contract is disclosure, not enforcement.
PLAN_CHANGE_FORM = register_form_contract(
    FormContract(
        key="customer.plan_change",
        title="Change plan",
        entity="subscription",
        command_owner="service_intent.subscription_lifecycle_execution",
        consequences=(
            FormConsequence(
                "proration",
                "Switching today applies an immediate prorated charge or credit "
                "for the remainder of your current period",
            ),
            FormConsequence(
                "reprovision",
                "A network profile change is queued for remote provisioning and "
                "verification before your subscription changes",
            ),
            FormConsequence(
                "field_fulfillment",
                "A physical access change is queued for field fulfillment; a work "
                "order is created only when a site visit is actually required",
            ),
        ),
    )
)


def _plan_change_prerequisites(
    subscription, available_offers, arrears_amount
) -> list[FormPrerequisite]:
    """Evaluate the plan-change preconditions the submit command enforces."""
    status_value = getattr(subscription.status, "value", subscription.status)
    return [
        FormPrerequisite(
            key="subscription_active",
            label="Your subscription is active",
            met=str(status_value) == "active",
            reason=(
                None
                if str(status_value) == "active"
                else "Plans can only be changed on an active subscription"
            ),
        ),
        FormPrerequisite(
            key="no_arrears",
            label="No overdue balance",
            met=arrears_amount <= Decimal("0.00"),
            reason=(
                None
                if arrears_amount <= Decimal("0.00")
                else "Clear your overdue balance before changing plans"
            ),
        ),
        FormPrerequisite(
            key="offers_available",
            label="Plans are available to switch to",
            met=len(available_offers) > 0,
            reason=(
                None
                if available_offers
                else "No alternative plans are currently available for your service"
            ),
        ),
    ]


def get_change_plan_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Get change plan page data for the customer portal."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None

    account_id = optional_customer_account_id(db, customer)
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    available_offers = get_available_portal_offers(db, subscription)

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)
    next_billing_date = _resolve_next_billing_date(db, subscription)
    copy = get_plan_change_copy(subscription)
    financial_position = _customer_financial_position(
        db, str(subscription.subscriber_id)
    )
    prepaid_funding = (
        financial_position.prepaid_available_balance
        if financial_position is not None
        else Decimal("0.00")
    )
    # Surface arrears up-front: a self-service plan change is blocked at submit
    # while the account has overdue invoices (block-until-settled, #30). Showing
    # it here lets the customer settle first instead of hitting the error on
    # submit.
    arrears_amount = (
        financial_position.collection_blocking_balance
        if financial_position is not None
        else Decimal("0.00")
    )
    # Plan-change quotes are computed lazily — one per offer the customer
    # selects — via get_plan_change_quote(). Pricing the whole catalog here ran a
    # proration calc per available offer, making this page take ~46s for large
    # catalogs and time out on submit (which could saturate workers / crash the app).
    quote_map: dict[str, dict[str, object]] = {}
    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "available_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in available_offers
        },
        "available_offers": available_offers,
        "available_offer_delivery_modes": _offer_delivery_modes(
            current_offer, available_offers
        ),
        "available_offer_change_quotes": quote_map,
        "service_addresses": _service_address_options(db, subscription),
        "current_service_address_id": (
            str(subscription.service_address_id)
            if subscription.service_address_id
            else None
        ),
        "prepaid_funding": prepaid_funding,
        "postpaid_receivables": (
            financial_position.open_invoice_balance
            if financial_position is not None
            else Decimal("0.00")
        ),
        "collection_blocking_balance": arrears_amount,
        "selected_offer_id": None,
        "insufficient_funding": None,
        "next_billing_date": next_billing_date,
        "arrears_amount": _to_float(arrears_amount),
        "in_arrears": arrears_amount > Decimal("0.00"),
        "form_contract": PLAN_CHANGE_FORM.state(
            _plan_change_prerequisites(subscription, available_offers, arrears_amount)
        ),
        **copy,
    }


def get_plan_change_quote(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
    target_service_address_id: str | None = None,
) -> dict | None:
    """Build the prorated plan-change quote for a single target offer (lazy).

    Replaces the per-offer upfront computation in get_change_plan_page so the
    change-plan page renders without pricing the whole catalog.

    Returns the serialized quote dict, ``{}`` when the subscription is not
    prepaid (no proration applies), or ``None`` when the subscription/offer is
    not found, not owned by this customer, or not a valid change target.
    """
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None

    account_id = optional_customer_account_id(db, customer)
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    if str(target_service_address_id or "") == str(
        subscription.service_address_id or ""
    ):
        target_service_address_id = None

    # Only quote offers that are actually offered to this subscription.
    available_offers = get_available_portal_offers(db, subscription)
    target_offer = next(
        (o for o in available_offers if str(o.id) == str(offer_id)), None
    )
    address_changes = bool(
        target_service_address_id
        and str(target_service_address_id) != str(subscription.service_address_id or "")
    )
    if target_offer is None or (
        str(target_offer.id) == str(subscription.offer_id) and not address_changes
    ):
        return None

    prepaid_funding = _customer_credit_balance(db, str(subscription.subscriber_id))
    return _serialize_plan_change_quote(
        _build_plan_change_quote(
            db,
            subscription,
            target_offer,
            prepaid_funding_before=prepaid_funding,
            target_service_address_id=target_service_address_id,
        )
    )


def submit_change_plan(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
    effective_date: str,
    notes: str | None = None,
) -> dict:
    """Submit a plan change request."""
    from app.services import subscription_changes as change_service

    # Ownership check (mirrors get_change_plan_page/get_plan_change_quote): the
    # subscription must belong to the caller, otherwise a customer could submit a
    # plan change against another subscriber's service (IDOR).
    account_id = optional_customer_account_id(db, customer)
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if (
        not subscription
        or not account_id
        or str(subscription.subscriber_id) != str(account_id)
    ):
        raise ValueError("Service not found.")

    subscriber_id = optional_customer_subscriber_id(db, customer)
    subscriber = (
        db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
    )

    # Fail fast on offers the customer could never change to (hidden, archived,
    # region-incompatible, reseller-restricted): apply-time validation would only
    # surface the rejection after the request sat in the approval queue.
    available = get_available_portal_offers(db, subscription)
    if str(offer_id) not in {str(offer.id) for offer in available}:
        raise ValueError("This plan is not available for self-service change.")

    # Same block-until-settled policy as confirm_service_change: an account
    # in arrears must clear overdue invoices before any plan change (including a
    # future-dated request).
    arrears = _collection_blocking_balance(db, str(subscription.subscriber_id))
    if arrears > Decimal("0.00"):
        raise ValueError(
            f"You have an overdue balance of {arrears:,.2f}. "
            "Please settle it before changing your plan."
        )

    eff_date = datetime.strptime(effective_date, "%Y-%m-%d").date()
    if eff_date < date.today():
        raise ValueError("Effective date must be today or later.")

    change_service.subscription_change_requests.create(
        db=db,
        subscription_id=subscription_id,
        new_offer_id=offer_id,
        effective_date=eff_date,
        requested_by_person_id=str(subscriber.id) if subscriber else None,
        notes=notes,
    )
    return {"success": True}


def get_change_plan_error_context(
    db: Session,
    subscription_id: str,
    *,
    selected_offer_id: str | None = None,
    insufficient_funding: dict[str, object] | None = None,
) -> dict:
    """Get context data for re-rendering the change plan form after an error."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    page_data = (
        get_change_plan_page(
            db,
            {"account_id": str(subscription.subscriber_id)} if subscription else {},
            subscription_id,
        )
        if subscription
        else None
    )
    available_offers = get_available_portal_offers(db, subscription)
    current_offer = (
        db.get(CatalogOffer, subscription.offer_id)
        if subscription and subscription.offer_id
        else None
    )
    next_billing_date = _resolve_next_billing_date(db, subscription)
    copy = get_plan_change_copy(subscription)
    error_position = (
        _customer_financial_position(db, str(subscription.subscriber_id))
        if subscription
        else None
    )

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "available_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in available_offers
        },
        "available_offers": available_offers,
        "available_offer_delivery_modes": _offer_delivery_modes(
            current_offer, available_offers
        ),
        "available_offer_change_quotes": (
            page_data.get("available_offer_change_quotes", {})
            if page_data is not None
            else {}
        ),
        "service_addresses": (
            _service_address_options(db, subscription) if subscription else []
        ),
        "current_service_address_id": (
            str(subscription.service_address_id)
            if subscription and subscription.service_address_id
            else None
        ),
        "prepaid_funding": (
            error_position.prepaid_available_balance
            if error_position is not None
            else Decimal("0.00")
        ),
        "postpaid_receivables": (
            error_position.open_invoice_balance
            if error_position is not None
            else Decimal("0.00")
        ),
        "collection_blocking_balance": (
            error_position.collection_blocking_balance
            if error_position is not None
            else Decimal("0.00")
        ),
        "selected_offer_id": selected_offer_id,
        "insufficient_funding": insufficient_funding,
        "next_billing_date": next_billing_date,
        "arrears_amount": (
            page_data.get("arrears_amount", 0.0) if page_data is not None else 0.0
        ),
        "in_arrears": (
            bool(page_data.get("in_arrears", False)) if page_data is not None else False
        ),
        **copy,
    }


def get_change_requests_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get change requests page data for the customer portal."""
    from app.services import subscription_changes as change_service

    account_id = optional_customer_account_id(db, customer)
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, object] = {
        "change_requests": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    change_requests = change_service.subscription_change_requests.list(
        db=db,
        subscription_id=None,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(SubscriptionChangeRequest.id)).where(
        SubscriptionChangeRequest.is_active.is_(True)
    )
    if account_id_str:
        stmt = stmt.join(Subscription).where(
            Subscription.subscriber_id == coerce_uuid(account_id_str)
        )
    if status:
        stmt = stmt.where(
            SubscriptionChangeRequest.status
            == _validate_enum(status, SubscriptionChangeStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "change_requests": change_requests,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def _get_offer_recurring_price(offer: CatalogOffer) -> Decimal:
    """Extract the first active recurring price from an offer."""
    from app.models.catalog import PriceType

    for price in offer.prices or []:
        if price.is_active and price.price_type == PriceType.recurring:
            return Decimal(str(price.amount))
    return Decimal("0")


def get_offer_price_summary(offer: CatalogOffer | None) -> SimpleNamespace:
    """Build recurring price display details for customer portal templates."""
    amount = Decimal("0")
    currency = "NGN"
    cycle_value = "monthly"

    if offer:
        for price in offer.prices or []:
            if price.is_active and price.price_type == PriceType.recurring:
                amount = Decimal(str(price.amount or 0))
                currency = str(price.currency or currency)
                raw_cycle = price.billing_cycle
                if raw_cycle:
                    cycle_value = (
                        raw_cycle.value
                        if hasattr(raw_cycle, "value")
                        else str(raw_cycle)
                    )
                break
        else:
            raw_cycle = getattr(offer, "billing_cycle", None)
            if raw_cycle:
                cycle_value = (
                    raw_cycle.value if hasattr(raw_cycle, "value") else str(raw_cycle)
                )

    return SimpleNamespace(
        amount=float(amount),
        amount_decimal=amount,
        currency=currency,
        billing_cycle=cycle_value,
        period_label=_CYCLE_LABELS.get(cycle_value, "/cycle"),
    )


def get_plan_change_copy(subscription: Subscription) -> dict[str, str]:
    """Return billing-mode-aware customer copy for plan changes."""
    billing_mode = getattr(subscription, "billing_mode", None)
    billing_mode_value = (
        billing_mode.value
        if billing_mode and hasattr(billing_mode, "value")
        else str(billing_mode or "")
    ).lower()
    if billing_mode_value == "prepaid":
        return {
            "timing_message": "Select a new plan. Changes take effect immediately.",
            "billing_message": (
                "Because this subscription is prepaid, an immediate upgrade uses "
                "prepaid funding for the prorated difference over the rest of this billing cycle."
            ),
        }
    return {
        "timing_message": "Select a new plan. Changes take effect immediately.",
        "billing_message": (
            "Your next invoice will reflect the new rate. No mid-cycle proration is applied "
            "for postpaid plans."
        ),
    }


def confirm_service_change(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
    notes: str | None = None,
    *,
    target_service_address_id: str | None = None,
    preview_fingerprint: str,
    field_quote_fingerprint: str | None = None,
    preview_effective_at: datetime | None = None,
    idempotency_key: str,
    confirmation_origin: str,
) -> dict:
    """Confirm a customer service change through the canonical delivery owner."""
    from fastapi import HTTPException

    from app.models.audit import AuditActorType
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionCommandOutcomeStatus,
        SubscriptionLifecycleCommand,
        resolve_subscription_lifecycle,
    )
    from app.services.subscription_lifecycle_commands import (
        confirm_subscription_service_change,
    )

    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        raise ValueError("Subscription not found")

    account_id = optional_customer_account_id(db, customer)
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription does not belong to this account")
    if str(target_service_address_id or "") == str(
        subscription.service_address_id or ""
    ):
        target_service_address_id = None

    replay = (
        db.query(SubscriptionChangeRequest)
        .filter(
            SubscriptionChangeRequest.confirmation_idempotency_key
            == idempotency_key.strip()
        )
        .one_or_none()
    )
    if replay is not None:
        if (
            str(replay.subscription_id) != str(subscription.id)
            or str(replay.requested_offer_id) != str(offer_id)
            or replay.confirmation_preview_fingerprint != preview_fingerprint.strip()
            or str(replay.target_service_address_id or "")
            != str(target_service_address_id or "")
        ):
            raise HTTPException(
                status_code=409,
                detail="Plan-change idempotency key belongs to another confirmation",
            )
        if replay.status in {
            SubscriptionChangeStatus.pending,
            SubscriptionChangeStatus.approved,
            SubscriptionChangeStatus.applied,
        }:
            confirmation = replay.confirmation_snapshot or {}
            pending = replay.status != SubscriptionChangeStatus.applied
            return {
                "success": True,
                "replayed": True,
                "status": "scheduled" if pending else "applied",
                "delivery_mode": str(
                    confirmation.get("delivery_mode") or "commercial_only"
                ),
                "message": (
                    "Existing service change is still awaiting delivery verification"
                    if pending
                    else "Service change was already applied"
                ),
                "change_request_id": str(replay.id),
                "account_adjustment_id": (
                    str(replay.account_adjustment_id)
                    if replay.account_adjustment_id
                    else None
                ),
                "credit_note_id": (
                    str(replay.credit_note_id) if replay.credit_note_id else None
                ),
                "ledger_entry_id": (
                    str(replay.ledger_entry_id) if replay.ledger_entry_id else None
                ),
            }

    new_offer = db.get(CatalogOffer, coerce_uuid(offer_id))
    if not new_offer:
        raise ValueError("Selected plan is not available")
    if not offer_has_positive_recurring_price(new_offer):
        raise ValueError("This plan is not available for self-service change.")
    # Gate on the same single source as the deferred path: this enforces
    # status==active + show_on_customer_portal + reseller
    # availability, not just is_active — otherwise the instant path could
    # switch a customer onto an archived or hidden offer by POSTing
    # its id directly (the deferred/mobile path was already guarded).
    available = get_available_portal_offers(db, subscription)
    if str(new_offer.id) not in {str(o.id) for o in available}:
        raise ValueError("This plan is not available for self-service change.")

    current_offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    # Block self-service plan changes while the account is in arrears. Policy:
    # the customer must settle overdue invoices first (covers prepaid AND
    # postpaid — the old affordability gate only looked at prepaid funding
    # and never considered outstanding debt). Account 100000016 could upgrade
    # while owing because this check did not exist.
    financial_position = _customer_financial_position(
        db, str(subscription.subscriber_id)
    )
    arrears = (
        financial_position.collection_blocking_balance
        if financial_position is not None
        else Decimal("0.00")
    )
    if arrears > Decimal("0.00"):
        raise ValueError(
            f"You have an overdue balance of {arrears:,.2f}. "
            "Please settle it before changing your plan."
        )

    old_price = (
        _get_offer_recurring_price(current_offer) if current_offer else Decimal("0")
    )
    new_price = _get_offer_recurring_price(new_offer)
    old_name = current_offer.name if current_offer else "Unknown"
    new_name = new_offer.name
    prepaid_funding = (
        financial_position.prepaid_available_balance
        if financial_position is not None
        else Decimal("0.00")
    )
    if preview_effective_at is None:
        raise HTTPException(
            status_code=400,
            detail="Plan-change preview effective timestamp is required",
        )
    quote = _build_plan_change_quote(
        db,
        subscription,
        new_offer,
        prepaid_funding_before=prepaid_funding,
        effective_at=preview_effective_at,
        target_service_address_id=target_service_address_id,
    )
    if str(quote.get("preview_fingerprint") or "") != preview_fingerprint.strip():
        raise HTTPException(
            status_code=409,
            detail="Financial state changed after preview; preview again",
        )
    required_amount = Decimal(str(quote["required_amount"]))
    shortfall = Decimal(str(quote["shortfall"]))
    is_prepaid = (
        getattr(
            subscription.billing_mode, "value", str(subscription.billing_mode or "")
        )
        == "prepaid"
    )

    if is_prepaid and required_amount > Decimal("0.00") and shortfall > Decimal("0.00"):
        return {
            "success": False,
            "reason": "insufficient_prepaid_funding",
            "selected_offer_id": str(new_offer.id),
            "plan_change_quote": quote,
            "required_amount": required_amount,
            "prepaid_funding_before": Decimal(str(quote["prepaid_funding_before"])),
            "shortfall": shortfall,
        }

    subscriber_id = optional_customer_subscriber_id(db, customer)
    subscriber = (
        db.get(Subscriber, coerce_uuid(subscriber_id)) if subscriber_id else None
    )

    reviewed = resolve_subscription_lifecycle(db, subscription_id)
    command = SubscriptionLifecycleCommand(
        subscription_id=subscription_id,
        kind=SubscriptionCommandKind.change_plan,
        source=confirmation_origin,
        effective_at=preview_effective_at,
        target_offer_id=offer_id,
        target_service_address_id=target_service_address_id,
        reason=notes or "Customer-confirmed plan change",
        expected_head=reviewed.head,
        expected_financial_fingerprint=preview_fingerprint,
        expected_field_quote_fingerprint=field_quote_fingerprint,
        idempotency_key=idempotency_key,
    )
    outcome = confirm_subscription_service_change(
        db,
        command,
        actor_id=str(subscriber.id) if subscriber else None,
        actor_type=(AuditActorType.user if subscriber else AuditActorType.system),
    )
    if outcome.status not in {
        SubscriptionCommandOutcomeStatus.applied,
        SubscriptionCommandOutcomeStatus.scheduled,
        SubscriptionCommandOutcomeStatus.skipped,
    }:
        status_code = (
            409
            if outcome.status == SubscriptionCommandOutcomeStatus.superseded
            or outcome.error_code
            in {"plan_change_financial_preview_stale", "subscription_head_changed"}
            else 400
        )
        raise HTTPException(status_code=status_code, detail=outcome.message)
    change_request = (
        db.get(SubscriptionChangeRequest, coerce_uuid(outcome.artifact_ids[0]))
        if outcome.artifact_ids
        else db.query(SubscriptionChangeRequest)
        .filter(
            SubscriptionChangeRequest.confirmation_idempotency_key
            == idempotency_key.strip()
        )
        .one_or_none()
    )
    if change_request is None:
        raise HTTPException(
            status_code=409,
            detail="Plan-change result evidence is unavailable",
        )

    return {
        "success": True,
        "status": outcome.status.value,
        "delivery_mode": str(quote.get("delivery_mode") or "unknown"),
        "message": outcome.message,
        "replayed": outcome.replayed,
        "old_offer_name": old_name,
        "new_offer_name": new_name,
        "old_price": old_price,
        "new_price": new_price,
        "price_difference": new_price - old_price,
        "proration": _serialize_plan_change_quote(quote),
        "change_request_id": str(change_request.id),
        "account_adjustment_id": (
            str(change_request.account_adjustment_id)
            if change_request.account_adjustment_id
            else None
        ),
        "credit_note_id": (
            str(change_request.credit_note_id)
            if change_request.credit_note_id
            else None
        ),
        "ledger_entry_id": (
            str(change_request.ledger_entry_id)
            if change_request.ledger_entry_id
            else None
        ),
    }


__all__ = [
    "get_change_plan_page",
    "submit_change_plan",
    "get_change_plan_error_context",
    "get_change_requests_page",
    "_get_offer_recurring_price",
    "get_offer_price_summary",
    "get_plan_change_copy",
    "get_plan_change_quote",
    "confirm_service_change",
]
