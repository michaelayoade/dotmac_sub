"""Plan change and change-request flows for customer portal."""

import logging
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, PriceType, Subscription
from app.models.subscriber import Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.services import catalog as catalog_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.customer_portal_context import get_available_portal_offers
from app.services.customer_portal_flow_common import (
    _compute_total_pages,
    _resolve_next_billing_date,
)

logger = logging.getLogger(__name__)

_CYCLE_LABELS = {
    "daily": "/day",
    "weekly": "/week",
    "monthly": "/month",
    "quarterly": "/quarter",
    "annual": "/year",
}

_PLAN_FAMILY_LABELS = {
    "unlimited": "Unlimited",
    "dedicated": "Dedicated",
    "home_flex": "Home Flex",
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
    from app.services.billing._common import get_account_credit_balance

    return get_account_credit_balance(db, account_id)


def _build_plan_change_quote(
    db: Session,
    subscription: Subscription,
    target_offer: CatalogOffer,
    *,
    current_balance: Decimal | None = None,
) -> dict[str, object]:
    from app.services.catalog.subscriptions import (
        _apply_plan_change_policy,
        _calculate_proration,
        _offer_recurring_price_amount,
    )
    from app.services.common import round_money

    proration = _calculate_proration(db, subscription, str(target_offer.id))
    proration = _apply_plan_change_policy(
        db,
        proration,
        old_price=_offer_recurring_price_amount(db, subscription.offer_id),
        new_price=_offer_recurring_price_amount(db, str(target_offer.id)),
    )
    balance = round_money(
        current_balance
        if current_balance is not None
        else _customer_credit_balance(db, str(subscription.subscriber_id))
    )
    required_amount = round_money(
        max(Decimal("0.00"), Decimal(str(proration.get("net_amount", "0.00"))))
    )
    shortfall = round_money(max(Decimal("0.00"), required_amount - balance))
    return {
        "current_remaining_value": round_money(
            Decimal(str(proration.get("credit_amount", "0.00")))
        ),
        "required_amount": required_amount,
        "current_balance": balance,
        "shortfall": shortfall,
        "charge_amount": round_money(
            Decimal(str(proration.get("charge_amount", "0.00")))
        ),
        "net_amount": round_money(Decimal(str(proration.get("net_amount", "0.00")))),
        "days_remaining": int(proration.get("days_remaining", 0) or 0),
        "days_in_cycle": int(proration.get("days_in_cycle", 0) or 0),
        "remaining_cycle_seconds": int(
            proration.get("remaining_cycle_seconds", 0) or 0
        ),
        "total_cycle_seconds": int(proration.get("total_cycle_seconds", 0) or 0),
        "can_apply_immediately": shortfall <= Decimal("0.00"),
        "is_upgrade": required_amount > Decimal("0.00"),
        "is_downgrade": Decimal(str(proration.get("net_amount", "0.00")))
        < Decimal("0.00"),
    }


def _serialize_plan_change_quote(quote: dict[str, object]) -> dict[str, object]:
    return {
        "current_remaining_value": _to_float(
            quote.get("current_remaining_value", Decimal("0.00"))
        ),
        "required_amount": _to_float(quote.get("required_amount", Decimal("0.00"))),
        "current_balance": _to_float(quote.get("current_balance", Decimal("0.00"))),
        "shortfall": _to_float(quote.get("shortfall", Decimal("0.00"))),
        "charge_amount": _to_float(quote.get("charge_amount", Decimal("0.00"))),
        "net_amount": _to_float(quote.get("net_amount", Decimal("0.00"))),
        "days_remaining": _to_int(quote.get("days_remaining", 0) or 0),
        "days_in_cycle": _to_int(quote.get("days_in_cycle", 0) or 0),
        "remaining_cycle_seconds": _to_int(
            quote.get("remaining_cycle_seconds", 0) or 0
        ),
        "total_cycle_seconds": _to_int(quote.get("total_cycle_seconds", 0) or 0),
        "can_apply_immediately": bool(quote.get("can_apply_immediately", False)),
        "is_upgrade": bool(quote.get("is_upgrade", False)),
        "is_downgrade": bool(quote.get("is_downgrade", False)),
    }


def _build_migration_options(
    db: Session,
    subscription: Subscription,
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for offer in _build_migration_offers(db, subscription):
        family = str(offer.plan_family or "").strip().lower()
        if family in seen:
            continue
        seen.add(family)
        options.append(
            {
                "family": family,
                "label": _PLAN_FAMILY_LABELS.get(
                    family, family.replace("_", " ").title()
                ),
            }
        )
    return options


def _build_migration_offers(
    db: Session,
    subscription: Subscription,
) -> list[CatalogOffer]:
    current_offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    if not current_offer:
        return []

    # No subscription arg on purpose (migration targets live in OTHER plan
    # families), but reseller scoping still applies via the subscriber.
    all_portal_offers = get_available_portal_offers(
        db, subscriber_id=subscription.subscriber_id
    )
    offers: list[CatalogOffer] = []
    for offer in all_portal_offers:
        family = str(offer.plan_family or "").strip().lower()
        if not family or family == str(current_offer.plan_family or "").strip().lower():
            continue
        if offer.service_type != current_offer.service_type:
            continue
        if offer.billing_mode != current_offer.billing_mode:
            continue
        if str(offer.region_zone_id or "") != str(current_offer.region_zone_id or ""):
            continue
        offers.append(offer)
    return offers


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

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    available_offers = get_available_portal_offers(db, subscription)

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)
    next_billing_date = _resolve_next_billing_date(db, subscription)
    copy = get_plan_change_copy(subscription)
    wallet_balance = _customer_credit_balance(db, str(subscription.subscriber_id))
    # Surface arrears up-front: a self-service plan change is blocked at submit
    # while the account has overdue invoices (block-until-settled, #30). Showing
    # it here lets the customer settle first instead of hitting the error on
    # submit.
    from app.services.payment_arrangements import get_account_outstanding_balance

    arrears_amount = get_account_outstanding_balance(
        db, str(subscription.subscriber_id)
    )
    # Plan-change quotes are computed lazily — one per offer the customer
    # selects — via get_plan_change_quote(). Pricing the whole catalog here ran a
    # proration calc per available offer, making this page take ~46s for large
    # catalogs and time out on submit (which could saturate workers / crash the app).
    quote_map: dict[str, dict[str, object]] = {}
    migration_offers = _build_migration_offers(db, subscription)

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "available_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in available_offers
        },
        "available_offers": available_offers,
        "migration_offers": migration_offers,
        "migration_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in migration_offers
        },
        "available_offer_change_quotes": quote_map,
        "current_wallet_balance": wallet_balance,
        "migration_options": _build_migration_options(db, subscription),
        "selected_offer_id": None,
        "insufficient_balance": None,
        "next_billing_date": next_billing_date,
        "arrears_amount": _to_float(arrears_amount),
        "in_arrears": arrears_amount > Decimal("0.00"),
        **copy,
    }


def get_plan_change_quote(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
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

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None

    # Only quote offers that are actually offered to this subscription.
    available_offers = get_available_portal_offers(db, subscription)
    target_offer = next(
        (o for o in available_offers if str(o.id) == str(offer_id)), None
    )
    if target_offer is None or str(target_offer.id) == str(subscription.offer_id):
        return None

    billing_mode_value = str(
        getattr(subscription.billing_mode, "value", subscription.billing_mode or "")
    )
    if billing_mode_value != "prepaid":
        return {}

    wallet_balance = _customer_credit_balance(db, str(subscription.subscriber_id))
    return _serialize_plan_change_quote(
        _build_plan_change_quote(
            db, subscription, target_offer, current_balance=wallet_balance
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
    account_id = customer.get("account_id")
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if (
        not subscription
        or not account_id
        or str(subscription.subscriber_id) != str(account_id)
    ):
        raise ValueError("Service not found.")

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    # Fail fast on offers the customer could never change to (cross-family,
    # hidden, archived, reseller-restricted): apply-time validation would only
    # surface the rejection after the request sat in the approval queue.
    available = get_available_portal_offers(db, subscription)
    if str(offer_id) not in {str(offer.id) for offer in available}:
        raise ValueError(
            "This plan is not available for self-service change. "
            "Contact support to migrate to it."
        )

    # Same block-until-settled policy as apply_instant_plan_change: an account
    # in arrears must clear overdue invoices before any plan change (including a
    # future-dated request).
    from app.services.payment_arrangements import get_account_outstanding_balance

    arrears = get_account_outstanding_balance(db, str(subscription.subscriber_id))
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
    insufficient_balance: dict[str, object] | None = None,
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
    migration_offers = _build_migration_offers(db, subscription) if subscription else []
    current_offer = (
        db.get(CatalogOffer, subscription.offer_id)
        if subscription and subscription.offer_id
        else None
    )
    next_billing_date = _resolve_next_billing_date(db, subscription)
    copy = get_plan_change_copy(subscription)

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "current_offer_summary": get_offer_price_summary(current_offer),
        "available_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in available_offers
        },
        "available_offers": available_offers,
        "migration_offers": migration_offers,
        "migration_offer_summaries": {
            str(offer.id): get_offer_price_summary(offer) for offer in migration_offers
        },
        "available_offer_change_quotes": (
            page_data.get("available_offer_change_quotes", {})
            if page_data is not None
            else {}
        ),
        "current_wallet_balance": (
            _customer_credit_balance(db, str(subscription.subscriber_id))
            if subscription
            else Decimal("0.00")
        ),
        "migration_options": _build_migration_options(db, subscription)
        if subscription
        else [],
        "selected_offer_id": selected_offer_id,
        "insufficient_balance": insufficient_balance,
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

    account_id = customer.get("account_id")
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
                "Because this subscription is prepaid, any same-family upgrade is charged "
                "from your wallet using the prorated difference for the rest of this billing cycle."
            ),
        }
    return {
        "timing_message": "Select a new plan. Changes take effect immediately.",
        "billing_message": (
            "Your next invoice will reflect the new rate. No mid-cycle proration is applied "
            "for postpaid plans."
        ),
    }


def apply_instant_plan_change(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
    notes: str | None = None,
) -> dict:
    """Instantly apply a plan change for the customer."""
    from fastapi import HTTPException

    from app.services import subscription_changes as change_service
    from app.services.catalog.subscriptions import (
        _create_prepaid_plan_change_debit,
        _validate_plan_change,
    )

    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        raise ValueError("Subscription not found")

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription does not belong to this account")

    # Serialize the wallet read-modify-write against concurrent add-on/autopay/
    # plan-change debits so the balance can't be overspent by a race.
    from app.services.billing._common import lock_account

    lock_account(db, str(subscription.subscriber_id))

    new_offer = db.get(CatalogOffer, coerce_uuid(offer_id))
    if not new_offer:
        raise ValueError("Selected plan is not available")
    # Gate on the same single source as the deferred path: this enforces
    # status==active + show_on_customer_portal + plan_family + reseller
    # availability, not just is_active — otherwise the instant path could
    # switch a customer onto an archived/hidden/cross-family offer by POSTing
    # its id directly (the deferred/mobile path was already guarded).
    available = get_available_portal_offers(db, subscription)
    if str(new_offer.id) not in {str(o.id) for o in available}:
        raise ValueError(
            "This plan is not available for self-service change. "
            "Contact support to migrate to it."
        )

    current_offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    try:
        _validate_plan_change(db, subscription, str(new_offer.id))
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc

    # Block self-service plan changes while the account is in arrears. Policy:
    # the customer must settle overdue invoices first (covers prepaid AND
    # postpaid — the old affordability gate only looked at prepaid wallet credit
    # and never considered outstanding debt). Account 100000016 could upgrade
    # while owing because this check did not exist.
    from app.services.payment_arrangements import get_account_outstanding_balance

    arrears = get_account_outstanding_balance(db, str(subscription.subscriber_id))
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
    wallet_balance = _customer_credit_balance(db, str(subscription.subscriber_id))
    quote = _build_plan_change_quote(
        db,
        subscription,
        new_offer,
        current_balance=wallet_balance,
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
            "reason": "insufficient_balance",
            "selected_offer_id": str(new_offer.id),
            "plan_change_quote": quote,
            "required_amount": required_amount,
            "current_balance": Decimal(str(quote["current_balance"])),
            "shortfall": shortfall,
        }

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    change_request = change_service.subscription_change_requests.create(
        db=db,
        subscription_id=subscription_id,
        new_offer_id=offer_id,
        effective_date=date.today(),
        requested_by_person_id=str(subscriber.id) if subscriber else None,
        notes=notes,
    )

    change_service.subscription_change_requests.approve(
        db=db,
        request_id=str(change_request.id),
        reviewer_id=None,
    )

    try:
        if is_prepaid and required_amount > Decimal("0.00"):
            _create_prepaid_plan_change_debit(
                db,
                subscription,
                required_amount,
                old_offer_name=old_name,
                new_offer_name=new_name,
            )
        change_service.subscription_change_requests.apply(
            db=db,
            request_id=str(change_request.id),
            skip_proration_artifacts=is_prepaid and required_amount > Decimal("0.00"),
        )
    except Exception:
        db.rollback()
        raise

    return {
        "success": True,
        "old_offer_name": old_name,
        "new_offer_name": new_name,
        "old_price": old_price,
        "new_price": new_price,
        "price_difference": new_price - old_price,
        "proration": _serialize_plan_change_quote(quote),
    }


def request_plan_migration(
    db: Session,
    customer: dict,
    subscription_id: str,
    *,
    target_family: str,
    requested_offer_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Create a support ticket for a cross-family migration request."""
    from app.services import crm_portal

    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        raise ValueError("Subscription not found")

    account_id = customer.get("account_id")
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription does not belong to this account")

    current_offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    requested_offer = (
        db.get(CatalogOffer, coerce_uuid(requested_offer_id))
        if requested_offer_id
        else None
    )
    subscriber_lookup = str(subscription.subscriber_id)
    title = "Request Plan Migration"
    description_lines = [
        f"Subscription: {subscription.id}",
        (
            f"Current offer: {current_offer.name if current_offer else subscription.offer_id}"
        ),
        (
            "Current family: "
            f"{str(getattr(current_offer, 'plan_family', '') or 'unclassified')}"
        ),
        f"Requested family: {target_family}",
    ]
    if requested_offer:
        description_lines.append(f"Requested offer: {requested_offer.name}")
    if notes:
        description_lines.extend(["", f"Customer notes: {notes.strip()}"])

    result = crm_portal.handle_ticket_create(
        db,
        customer,
        subscriber_lookup,
        title,
        "\n".join(description_lines),
        "normal",
    )
    if not result.get("success"):
        raise ValueError(result.get("error") or "Unable to create ticket.")
    return result


__all__ = [
    "get_change_plan_page",
    "submit_change_plan",
    "get_change_plan_error_context",
    "get_change_requests_page",
    "_get_offer_recurring_price",
    "get_offer_price_summary",
    "get_plan_change_copy",
    "get_plan_change_quote",
    "apply_instant_plan_change",
    "request_plan_migration",
]
