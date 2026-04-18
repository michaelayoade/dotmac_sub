"""Dashboard and shared context helpers for customer portal."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.models.catalog import CatalogOffer, PriceType, Subscription, SubscriptionStatus
from app.models.provisioning import InstallAppointment, ServiceOrder
from app.models.subscriber import (
    AccountStatus,
    Subscriber,
    SubscriberCategory,
    SubscriberStatus,
)
from app.models.support import Ticket, TicketStatus
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services.bandwidth import bandwidth_samples
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


def emit_customer_event(db: Session, event_name: str, payload: dict) -> None:
    """Emit a customer portal event without letting telemetry break the request."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        event_type = getattr(EventType, event_name, None)
        if event_type:
            emit_event(db, event_type, payload, actor="customer")
    except Exception as exc:
        logger.warning("Failed to emit customer event %s: %s", event_name, exc)


def resolve_subscriber_id(session: dict) -> str:
    """Extract subscriber_id from a customer session for CRM lookups."""
    return str(
        session.get("subscriber_id")
        or session.get("session", {}).get("subscriber_id", "")
    )


def resolve_allowed_subscriber_ids(session: dict, db: Session) -> list[str]:
    """Resolve all customer-visible subscriber/account IDs for access checks."""
    allowed = get_allowed_account_ids(session, db)
    if allowed:
        return [str(item) for item in allowed if item]
    fallback = resolve_subscriber_id(session)
    return [fallback] if fallback else []


def resolve_customer_subscription(db: Session, session: dict) -> Subscription | None:
    """Resolve the active subscription visible to the current customer session."""
    account_id, session_subscription_id = resolve_customer_account(session, db)
    account_id_str = str(account_id) if account_id else None

    if session_subscription_id:
        subscription = db.get(Subscription, session_subscription_id)
        if subscription and (
            not account_id_str or str(subscription.subscriber_id) == account_id_str
        ):
            return subscription

    if not account_id_str:
        return None

    try:
        return bandwidth_samples.get_user_active_subscription(
            db, {"account_id": account_id_str}
        )
    except HTTPException:
        return None


def get_dashboard_template_context(db: Session, session: dict) -> tuple[str, dict]:
    """Return the dashboard template and context for full or restricted access."""
    subscriber_id = session.get("subscriber_id")
    if subscriber_id and is_subscriber_restricted(db, subscriber_id):
        return (
            "customer/dashboard/restricted.html",
            {
                "customer": session,
                **get_restricted_dashboard_context(db, session),
                "active_page": "dashboard",
            },
        )
    return (
        "customer/dashboard/index.html",
        {
            "customer": session,
            **get_dashboard_context(db, session),
            "active_page": "dashboard",
        },
    )


def _format_address(address) -> str:
    if not address:
        return "No address on file"
    parts = [address.address_line1]
    if address.city:
        parts.append(address.city)
    if address.region:
        parts.append(address.region)
    if address.postal_code:
        parts.append(address.postal_code)
    return ", ".join([part for part in parts if part])


def _get_subscriber_devices(db: Session, subscriber_id: str) -> list:
    """Get subscriber's ONT devices using the subscriber-ONT adapter.

    Returns a list of SimpleNamespace objects with device info for the portal.
    """
    try:
        from app.services.network.subscriber_ont_adapter import get_subscriber_onts

        onts = get_subscriber_onts(db, subscriber_id)
        devices = []
        for ont_info in onts:
            # Map online status to user-friendly display
            status_display = {
                "online": "Online",
                "offline": "Offline",
                "unknown": "Unknown",
            }.get(ont_info.online_status or "unknown", "Unknown")

            devices.append(
                SimpleNamespace(
                    serial_number=ont_info.serial_number or "Unknown",
                    model=ont_info.model or "ONT Device",
                    status=ont_info.online_status or "unknown",
                    status_display=status_display,
                    location=ont_info.service_address or "Service address",
                )
            )
        return devices
    except Exception as exc:
        logger.warning(
            "Failed to get devices for subscriber %s: %s",
            subscriber_id,
            exc,
        )
        return []


def get_dashboard_context(db: Session, session: dict) -> dict:
    account_id = session.get("account_id")
    subscriber_id = session.get("subscriber_id")

    account_obj = None
    if account_id:
        try:
            account_obj = subscriber_service.accounts.get(db, account_id)
        except Exception:
            account_obj = None

    subscriber = None
    if subscriber_id:
        subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber and account_obj:
        subscriber = account_obj.subscriber

    user_name = session.get("username") or "Customer"
    user = {"first_name": user_name}
    if subscriber:
        if subscriber.category == SubscriberCategory.business:
            user = {
                "first_name": subscriber.company_name
                or subscriber.display_name
                or subscriber.first_name
            }
        elif subscriber.first_name:
            user = {"first_name": subscriber.first_name}

    invoices = []
    if account_id:
        invoices = billing_service.invoices.list(
            db=db,
            account_id=account_id,
            status=None,
            is_active=None,
            order_by="issued_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )

    balance = sum(float(inv.balance_due or 0) for inv in invoices)
    next_bill_amount = float(invoices[0].total or 0) if invoices else 0.0
    next_bill_date = None

    subscriptions = []
    if account_id:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=account_id,
            offer_id=None,
            status=SubscriptionStatus.active.value,
            order_by="created_at",
            order_dir="desc",
            limit=25,
            offset=0,
        )

    if subscriptions:
        next_bill_date = subscriptions[0].next_billing_at
    if not next_bill_date and invoices:
        next_bill_date = invoices[0].due_at or invoices[0].issued_at
    if not next_bill_date:
        next_bill_date = datetime.now(UTC) + timedelta(days=30)

    account = SimpleNamespace(
        balance=balance,
        next_bill_amount=next_bill_amount,
        next_bill_date=next_bill_date,
    )

    services = []
    for subscription in subscriptions:
        offer = subscription.offer
        speed = "N/A"
        if offer and (offer.speed_download_mbps or offer.speed_upload_mbps):
            speed = f"{offer.speed_download_mbps or '-'}/{offer.speed_upload_mbps or '-'} Mbps"
        address = _format_address(subscription.service_address)
        recurring_prices = []
        if offer:
            recurring_prices = [
                price
                for price in offer.prices
                if price.price_type == PriceType.recurring and price.is_active
            ]
        monthly_cost = float(recurring_prices[0].amount) if recurring_prices else 0.0
        services.append(
            SimpleNamespace(
                name=offer.name if offer else "Service",
                speed=speed,
                address=address,
                status=subscription.status.value if subscription.status else "pending",
                monthly_cost=monthly_cost,
            )
        )

    primary_service = (
        services[0]
        if services
        else SimpleNamespace(
            status="inactive",
            plan_name="No active plan",
        )
    )
    if services:
        primary_service = SimpleNamespace(
            status=services[0].status,
            plan_name=services[0].name,
        )

    open_count = 0
    if account_id:
        try:
            open_count = (
                db.query(func.count(Ticket.id))
                .filter(Ticket.is_active.is_(True))
                .filter(
                    (Ticket.subscriber_id == account_id)
                    | (Ticket.customer_account_id == account_id)
                )
                .filter(
                    Ticket.status.notin_(
                        (
                            TicketStatus.closed,
                            TicketStatus.canceled,
                            TicketStatus.merged,
                        )
                    )
                )
                .scalar()
                or 0
            )
        except Exception:
            open_count = 0

    # Billing mode and prepaid balance
    billing_mode = "postpaid"
    prepaid_balance = 0.0
    if subscriber and hasattr(subscriber, "billing_mode") and subscriber.billing_mode:
        billing_mode = subscriber.billing_mode.value
    if billing_mode == "prepaid" and account_id:
        try:
            from app.services.collections._core import (
                _resolve_prepaid_available_balance,
            )

            prepaid_balance = float(
                _resolve_prepaid_available_balance(db, str(account_id))
            )
        except Exception:
            logger.warning(
                "Failed to resolve prepaid balance for account %s",
                account_id,
                exc_info=True,
            )
            prepaid_balance = 0.0

    # Get subscriber's ONT devices
    devices = []
    if subscriber_id:
        devices = _get_subscriber_devices(db, subscriber_id)

    return {
        "user": SimpleNamespace(**user),
        "account": account,
        "service": primary_service,
        "services": services,
        "devices": devices,
        "tickets": SimpleNamespace(open_count=open_count),
        "recent_activity": [],
        "billing_mode": billing_mode,
        "prepaid_balance": prepaid_balance,
    }


_RESTRICTED_STATUSES = {
    SubscriberStatus.blocked,
    SubscriberStatus.suspended,
    SubscriberStatus.disabled,
}

STATUS_DISPLAY = {
    "blocked": "Blocked — Non-payment",
    "suspended": "Suspended",
    "disabled": "Disabled by administrator",
    "canceled": "Canceled",
}


def get_restricted_since(subscriber: Subscriber) -> datetime | None:
    """Return when the subscriber most recently entered a restricted status."""
    metadata = subscriber.metadata_ or {}
    return subscriber_service._metadata_datetime(metadata, "restricted_since")  # type: ignore[attr-defined]


def get_total_outstanding_balance(db: Session, account_id: object) -> float:
    """Sum all active positive invoice balances for the account."""
    total = (
        db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
        .filter(Invoice.account_id == coerce_uuid(account_id))
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.balance_due > 0)
        .scalar()
    )
    return float(total or 0)


def is_subscriber_restricted(db: Session, subscriber_id: object) -> bool:
    """Check if a subscriber should see the restricted portal view."""
    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber:
        return False
    return subscriber.status in _RESTRICTED_STATUSES


def get_restricted_dashboard_context(db: Session, session: dict) -> dict:
    """Build context for the restricted/expired subscriber portal view."""
    subscriber_id = session.get("subscriber_id")
    account_id = session.get("account_id")

    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None
    if not subscriber:
        return {"restricted": True, "account_status": "Unknown"}

    user_name = session.get("username") or "Customer"
    if subscriber.category == SubscriberCategory.business:
        user_name = subscriber.company_name or subscriber.display_name or user_name
    elif subscriber.first_name:
        user_name = f"{subscriber.first_name} {subscriber.last_name or ''}".strip()

    # Outstanding balance from invoices
    balance = 0.0
    recent_invoices = []
    if account_id:
        balance = get_total_outstanding_balance(db, account_id)
        invoices = billing_service.invoices.list(
            db=db,
            account_id=account_id,
            status=None,
            is_active=None,
            order_by="issued_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )
        recent_invoices = [inv for inv in invoices if float(inv.balance_due or 0) > 0][
            :3
        ]

    # Subscriptions (all, not just active)
    subscriptions = []
    if account_id:
        subscriptions = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=account_id,
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )

    plan_name = None
    if subscriptions and subscriptions[0].offer:
        plan_name = subscriptions[0].offer.name

    status_value = subscriber.status.value if subscriber.status else "unknown"

    return {
        "restricted": True,
        "user_name": user_name,
        "subscriber_number": subscriber.subscriber_number or subscriber.account_number,
        "account_status": status_value,
        "account_status_display": STATUS_DISPLAY.get(
            status_value, status_value.title()
        ),
        "plan_name": plan_name,
        "outstanding_balance": balance,
        "recent_invoices": recent_invoices,
        "account_start_date": subscriber.account_start_date,
        "blocked_since": get_restricted_since(subscriber),
        "email": subscriber.email,
        "phone": subscriber.phone,
    }


def resolve_customer_account(
    customer: dict, db: Session
) -> tuple[str | None, str | None]:
    """Resolve account_id and subscription_id from customer session.

    Args:
        customer: Customer session dict
        db: Database session

    Returns:
        Tuple of (account_id_str, subscription_id_str)
    """
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None
    if account_id_str or subscription_id_str:
        return account_id_str, subscription_id_str

    subscriber_id = customer.get("subscriber_id")
    if not subscriber_id:
        return None, None
    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=str(subscriber_id),
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    if not accounts:
        return None, subscription_id_str
    active_account = next(
        (account for account in accounts if account.status == AccountStatus.active),
        None,
    )
    account = active_account or accounts[0]
    return str(account.id), subscription_id_str


def get_allowed_account_ids(customer: dict, db: Session) -> list[str]:
    """Get list of account IDs the customer has access to.

    Args:
        customer: Customer session dict
        db: Database session

    Returns:
        List of account ID strings
    """
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    subscriber = None
    subscriber_id = customer.get("subscriber_id")
    if subscriber_id:
        subscriber = db.get(Subscriber, subscriber_id)

    allowed_account_ids = []
    if subscriber:
        allowed_account_ids = [str(subscriber.id)]
    if account_id_str and account_id_str not in allowed_account_ids:
        allowed_account_ids.append(account_id_str)

    return allowed_account_ids


def get_invoice_billing_contact(db: Session, invoice, customer: dict) -> dict:
    """Resolve billing name and email for an invoice.

    Args:
        db: Database session
        invoice: Invoice model instance
        customer: Customer session dict

    Returns:
        Dict with 'billing_name' and 'billing_email' keys
    """
    billing_name = None
    billing_email = None
    account = None

    if invoice.account_id:
        try:
            account = subscriber_service.accounts.get(
                db=db, account_id=str(invoice.account_id)
            )
        except Exception:
            account = None

    if account:
        billing_name = (
            account.company_name
            if account.category == SubscriberCategory.business
            else account.display_name
            or f"{account.first_name} {account.last_name}".strip()
        )
        billing_email = account.email

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    if not billing_name and subscriber:
        billing_name = (
            subscriber.company_name
            if subscriber.category == SubscriberCategory.business
            else subscriber.display_name
            or f"{subscriber.first_name} {subscriber.last_name}".strip()
        )
        billing_email = billing_email or subscriber.email

    current_user = customer.get("current_user") if isinstance(customer, dict) else None
    if current_user:
        billing_name = billing_name or current_user.get("name")
        billing_email = billing_email or current_user.get("email")

    return {"billing_name": billing_name, "billing_email": billing_email}


def get_customer_appointments(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get installation appointments for the customer with pagination.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'appointments', 'total', 'total_pages' keys
    """
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    if not account_id_str and not subscription_id_str:
        return {"appointments": [], "total": 0, "total_pages": 1}

    filters = []
    if account_id_str:
        filters.append(ServiceOrder.subscriber_id == coerce_uuid(account_id_str))
    if subscription_id_str:
        filters.append(ServiceOrder.subscription_id == coerce_uuid(subscription_id_str))
    if status:
        filters.append(InstallAppointment.status == status)

    count_stmt = (
        select(func.count(InstallAppointment.id))
        .join(ServiceOrder, InstallAppointment.service_order_id == ServiceOrder.id)
        .where(*filters)
    )
    total = db.scalar(count_stmt) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    start = (page - 1) * per_page

    list_stmt = (
        select(InstallAppointment)
        .join(ServiceOrder, InstallAppointment.service_order_id == ServiceOrder.id)
        .where(*filters)
        .order_by(InstallAppointment.scheduled_start.desc())
        .offset(start)
        .limit(per_page)
    )
    appointments = db.scalars(list_stmt).all()

    return {"appointments": appointments, "total": total, "total_pages": total_pages}


def get_available_portal_offers(db: Session) -> list[CatalogOffer]:
    """Get catalog offers available on the customer portal.

    Args:
        db: Database session

    Returns:
        List of CatalogOffer instances
    """
    return cast(
        list[CatalogOffer],
        db.scalars(
            select(CatalogOffer)
            .where(CatalogOffer.is_active.is_(True))
            .where(CatalogOffer.show_on_customer_portal.is_(True))
            .order_by(CatalogOffer.name.asc())
        ).all(),
    )


def get_outstanding_balance(db: Session, account_id: str) -> dict:
    """Get outstanding balance and overdue invoices for an account.

    Args:
        db: Database session
        account_id: Account ID string

    Returns:
        Dict with 'invoices' and 'outstanding_balance' keys
    """
    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id,
        status="overdue",
        is_active=True,
        order_by="due_at",
        order_dir="asc",
        limit=50,
        offset=0,
    )
    outstanding_balance = sum(inv.balance_due or 0 for inv in invoices)

    return {"invoices": invoices, "outstanding_balance": outstanding_balance}
