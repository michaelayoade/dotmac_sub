"""Plan change and change-request flows for customer portal."""

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, Subscription
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
    if account_id and str(subscription.subscriber_id) != str(account_id):
        return None

    available_offers = get_available_portal_offers(db)

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)
    next_billing_date = _resolve_next_billing_date(db, subscription)

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "available_offers": available_offers,
        "next_billing_date": next_billing_date,
    }


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

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

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
) -> dict:
    """Get context data for re-rendering the change plan form after an error."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    available_offers = get_available_portal_offers(db)
    current_offer = (
        db.get(CatalogOffer, subscription.offer_id)
        if subscription and subscription.offer_id
        else None
    )
    next_billing_date = _resolve_next_billing_date(db, subscription)

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "available_offers": available_offers,
        "next_billing_date": next_billing_date,
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


def apply_instant_plan_change(
    db: Session,
    customer: dict,
    subscription_id: str,
    offer_id: str,
    notes: str | None = None,
) -> dict:
    """Instantly apply a plan change for the customer."""
    from app.services import subscription_changes as change_service
    from app.services.events import emit_event
    from app.services.events.types import EventType

    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        raise ValueError("Subscription not found")

    account_id = customer.get("account_id")
    if account_id and str(subscription.subscriber_id) != str(account_id):
        raise ValueError("Subscription does not belong to this account")

    new_offer = db.get(CatalogOffer, coerce_uuid(offer_id))
    if not new_offer or not new_offer.is_active:
        raise ValueError("Selected plan is not available")

    current_offer = (
        db.get(CatalogOffer, subscription.offer_id) if subscription.offer_id else None
    )
    old_price = (
        _get_offer_recurring_price(current_offer) if current_offer else Decimal("0")
    )
    new_price = _get_offer_recurring_price(new_offer)
    old_name = current_offer.name if current_offer else "Unknown"
    new_name = new_offer.name

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

    change_service.subscription_change_requests.apply(
        db=db,
        request_id=str(change_request.id),
    )

    event_type = (
        EventType.subscription_upgraded
        if new_price > old_price
        else EventType.subscription_downgraded
    )
    emit_event(
        db,
        event_type,
        {
            "subscription_id": subscription_id,
            "old_offer": old_name,
            "new_offer": new_name,
            "price_difference": str(new_price - old_price),
        },
    )

    return {
        "old_offer_name": old_name,
        "new_offer_name": new_name,
        "old_price": old_price,
        "new_price": new_price,
        "price_difference": new_price - old_price,
    }


__all__ = [
    "get_change_plan_page",
    "submit_change_plan",
    "get_change_plan_error_context",
    "get_change_requests_page",
    "_get_offer_recurring_price",
    "apply_instant_plan_change",
]
