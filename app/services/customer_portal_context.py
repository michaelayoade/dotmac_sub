"""Dashboard and shared context helpers for customer portal."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, PriceType
from app.models.provisioning import InstallAppointment, ServiceOrder
from app.models.subscriber import AccountStatus, Organization, Subscriber
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


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
        if subscriber.first_name:
            user = {"first_name": subscriber.first_name}
        elif subscriber.organization_id:
            organization = db.get(Organization, subscriber.organization_id)
            if organization and organization.name:
                user = {"first_name": organization.name}

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
            status=None,
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
            speed = f"{offer.speed_download_mbps or '-'}/{offer.speed_upload_mbps or '-'} Kbps"
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

    # Tickets module removed - always return 0
    open_count = 0

    return {
        "user": SimpleNamespace(**user),
        "account": account,
        "service": primary_service,
        "services": services,
        "tickets": SimpleNamespace(open_count=open_count),
        "recent_activity": [],
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
            account.display_name or f"{account.first_name} {account.last_name}".strip()
        )
        billing_email = account.email
        if account.organization_id:
            organization = db.get(Organization, account.organization_id)
            if organization:
                billing_name = organization.name

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    if not billing_name and subscriber:
        billing_name = (
            subscriber.display_name
            or f"{subscriber.first_name} {subscriber.last_name}".strip()
        )
        billing_email = billing_email or subscriber.email
        if subscriber.organization_id:
            organization = db.get(Organization, subscriber.organization_id)
            if organization:
                billing_name = organization.name

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
