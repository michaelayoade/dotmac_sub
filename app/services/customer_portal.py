import logging
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    PriceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.payment_arrangement import ArrangementStatus, PaymentArrangement
from app.models.provisioning import InstallAppointment, ServiceOrder, ServiceOrderStatus
from app.models.radius import RadiusUser
from app.models.subscriber import AccountStatus, Organization, Subscriber
from app.models.subscription_change import (
    SubscriptionChangeRequest,
    SubscriptionChangeStatus,
)
from app.models.usage import UsageRecord
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import provisioning as provisioning_service
from app.services import subscriber as subscriber_service
from app.services.common import coerce_uuid
from app.services.common import validate_enum as _validate_enum
from app.services.session_store import delete_session, load_session, store_session
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "customer_session"
# Default values for fallback when db is not available
_DEFAULT_SESSION_TTL = 86400  # 24 hours
_DEFAULT_REMEMBER_TTL = 2592000  # 30 days

_CUSTOMER_SESSIONS: dict[str, dict] = {}
_CUSTOMER_SESSION_PREFIX = "session:customer_portal"


def _parse_setting_int(value: object | None, default: int) -> int:
    """Parse a setting value into an int, falling back to default on bad inputs."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            return default
    return default


def create_customer_session(
    username: str,
    account_id: UUID | None,
    subscriber_id: UUID | None,
    subscription_id: UUID | None = None,
    return_to: str | None = None,
    remember: bool = False,
    db: Session | None = None,
) -> str:
    """Create a new customer session and return the session token."""
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    session_payload = {
        "username": username,
        "account_id": str(account_id) if account_id else None,
        "subscriber_id": str(subscriber_id) if subscriber_id else None,
        "subscription_id": str(subscription_id) if subscription_id else None,
        "return_to": return_to,
        "remember": remember,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    store_session(
        _CUSTOMER_SESSION_PREFIX,
        session_token,
        session_payload,
        ttl_seconds,
        _CUSTOMER_SESSIONS,
    )
    return session_token


def get_customer_session(session_token: str) -> dict | None:
    """Get customer session data from token."""
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    if not session:
        return None

    # Check expiration
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(UTC) > expires_at:
        invalidate_customer_session(session_token)
        return None

    return session


def refresh_customer_session(session_token: str, db: Session | None = None) -> dict | None:
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    if not session:
        return None

    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(UTC) > expires_at:
        invalidate_customer_session(session_token)
        return None

    ttl_seconds = _session_ttl_seconds(session.get("remember", False), db)
    session["expires_at"] = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    store_session(
        _CUSTOMER_SESSION_PREFIX,
        session_token,
        session,
        ttl_seconds,
        _CUSTOMER_SESSIONS,
    )
    return session


def invalidate_customer_session(session_token: str) -> None:
    """Invalidate a customer session."""
    delete_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)


def get_current_customer(session_token: str | None, db: Session) -> dict | None:
    """Resolve a customer session token into a hydrated session dict."""
    if not session_token:
        return None

    session = get_customer_session(session_token)
    if not session:
        return None

    # Enrich session with user data
    username = session.get("username")
    if username:
        radius_user = db.scalars(
            select(RadiusUser)
            .where(RadiusUser.username == username)
            .where(RadiusUser.is_active.is_(True))
        ).first()
        if radius_user:
            session["radius_user_id"] = str(radius_user.id)
            if radius_user.subscriber_id:
                session["account_id"] = str(radius_user.subscriber_id)
            if radius_user.subscription_id:
                session["subscription_id"] = str(radius_user.subscription_id)
        else:
            credential = db.scalars(
                select(AccessCredential)
                .where(AccessCredential.username == username)
                .where(AccessCredential.is_active.is_(True))
            ).first()
            if credential:
                session["account_id"] = str(credential.subscriber_id)
                session["subscriber_id"] = str(credential.subscriber_id)
            else:
                local_credential = db.scalars(
                    select(UserCredential)
                    .where(UserCredential.username == username)
                    .where(UserCredential.provider == AuthProvider.local)
                    .where(UserCredential.is_active.is_(True))
                ).first()
                if local_credential:
                    subscriber = db.scalars(
                        select(Subscriber)
                        .where(Subscriber.id == local_credential.subscriber_id)
                        .where(Subscriber.is_active.is_(True))
                    ).first()
                    if subscriber:
                        session["subscriber_id"] = str(subscriber.id)
                        session["account_id"] = str(subscriber.id)

    subscription_id = session.get("subscription_id")
    if subscription_id and session.get("account_id") is None:
        subscription = db.get(Subscription, subscription_id)
        if subscription and subscription.subscriber_id:
            session["account_id"] = str(subscription.subscriber_id)
            session["subscriber_id"] = str(subscription.subscriber_id)

    session["current_user"] = _build_current_user(db, session)
    return session


def _session_ttl_seconds(remember: bool, db: Session | None = None) -> int:
    """Get session TTL in seconds, using configurable settings when db is available."""
    if remember:
        ttl = (
            resolve_value(db, SettingDomain.auth, "customer_remember_ttl_seconds")
            if db
            else None
        )
        return _parse_setting_int(ttl, _DEFAULT_REMEMBER_TTL)
    else:
        ttl = (
            resolve_value(db, SettingDomain.auth, "customer_session_ttl_seconds")
            if db
            else None
        )
        return _parse_setting_int(ttl, _DEFAULT_SESSION_TTL)


def get_session_max_age(db: Session | None = None) -> int:
    """Get the session max age for non-remember sessions."""
    return _session_ttl_seconds(remember=False, db=db)


def get_remember_max_age(db: Session | None = None) -> int:
    """Get the session max age for remember-me sessions."""
    return _session_ttl_seconds(remember=True, db=db)


def _build_current_user(db: Session, session: dict) -> dict:
    subscriber = None
    subscriber_id = session.get("subscriber_id")
    if subscriber_id:
        subscriber = db.get(Subscriber, subscriber_id)
    name = session.get("username") or "Customer"
    email = None
    if subscriber:
        name = subscriber.display_name or f"{subscriber.first_name} {subscriber.last_name}".strip() or name
        email = subscriber.email or email
        if subscriber.organization_id:
            organization = db.get(Organization, subscriber.organization_id)
            if organization and organization.name:
                name = organization.name
    if not email and session.get("username"):
        email = session.get("username")

    initials = "".join([part[:1] for part in name.split() if part]).upper()[:2] or "CU"
    return {"name": name, "email": email or "", "initials": initials}


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
                price for price in offer.prices
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

    primary_service = services[0] if services else SimpleNamespace(
        status="inactive",
        plan_name="No active plan",
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


def resolve_customer_account(customer: dict, db: Session) -> tuple[str | None, str | None]:
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
            account = subscriber_service.accounts.get(db=db, account_id=str(invoice.account_id))
        except Exception:
            account = None

    if account:
        billing_name = account.display_name or f"{account.first_name} {account.last_name}".strip()
        billing_email = account.email
        if account.organization_id:
            organization = db.get(Organization, account.organization_id)
            if organization:
                billing_name = organization.name

    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    if not billing_name and subscriber:
        billing_name = subscriber.display_name or f"{subscriber.first_name} {subscriber.last_name}".strip()
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
        filters.append(
            ServiceOrder.subscription_id == coerce_uuid(subscription_id_str)
        )
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


def _compute_total_pages(total: int, per_page: int) -> int:
    """Compute total pages from total count and per_page size."""
    return (total + per_page - 1) // per_page if total else 1


def _resolve_next_billing_date(db: Session, subscription: Any) -> date | None:
    """Resolve the next billing date for a subscription.

    Uses the subscription's next_billing_at if available, otherwise computes
    from the billing cycle of the associated offer.

    Args:
        db: Database session
        subscription: Subscription model instance (or None)

    Returns:
        The next billing date, or None if it cannot be resolved.
    """
    if not subscription:
        return None
    next_billing_at = getattr(subscription, "next_billing_at", None)
    if isinstance(next_billing_at, datetime):
        return next_billing_at.date()
    start_at = getattr(subscription, "start_at", None) or getattr(
        subscription, "created_at", None
    )
    offer_id = getattr(subscription, "offer_id", None)
    if not start_at or not offer_id:
        return None
    from app.services.catalog.subscriptions import (
        _compute_next_billing_at,
        _resolve_billing_cycle,
    )

    offer_version_id = getattr(subscription, "offer_version_id", None)
    cycle = _resolve_billing_cycle(
        db,
        str(offer_id),
        str(offer_version_id) if offer_version_id else None,
    )
    next_bill = cast(datetime, _compute_next_billing_at(start_at, cycle))
    for _ in range(240):
        if next_bill.date() >= date.today():
            break
        next_bill = cast(datetime, _compute_next_billing_at(next_bill, cycle))
    return next_bill.date()


def get_billing_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get billing page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional invoice status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'invoices', 'status', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    if status == "pending":
        status = "issued"

    empty_result: dict[str, Any] = {
        "invoices": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        is_active=None,
        order_by="issued_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = (
        select(func.count(Invoice.id))
        .where(Invoice.account_id == coerce_uuid(account_id_str))
        .where(Invoice.is_active.is_(True))
    )
    if status:
        stmt = stmt.where(
            Invoice.status == _validate_enum(status, InvoiceStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "invoices": invoices,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_usage_page(
    db: Session,
    customer: dict,
    period: str = "current",
    page: int = 1,
    per_page: int = 25,
) -> dict:
    """Get usage page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        period: Usage period filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'usage_records', 'period', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    from app.services import usage as usage_service

    subscription_id = customer.get("subscription_id")
    subscription_id_str = str(subscription_id) if subscription_id else None

    empty_result: dict[str, Any] = {
        "usage_records": [],
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not subscription_id_str:
        return empty_result

    usage_records = usage_service.usage_records.list(
        db=db,
        subscription_id=subscription_id_str,
        quota_bucket_id=None,
        order_by="recorded_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    total = (
        db.scalar(
            select(func.count(UsageRecord.id)).where(
                UsageRecord.subscription_id == coerce_uuid(subscription_id_str)
            )
        )
        or 0
    )

    return {
        "usage_records": usage_records,
        "period": period,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_services_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get services page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional subscription status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'services', 'status', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, Any] = {
        "services": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    services = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=account_id_str,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(Subscription.id)).where(
        Subscription.subscriber_id == coerce_uuid(account_id_str)
    )
    if status:
        stmt = stmt.where(
            Subscription.status
            == _validate_enum(status, SubscriptionStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "services": services,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_detail(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Get service detail data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        subscription_id: Subscription UUID string

    Returns:
        Dict with 'subscription', 'current_offer', 'next_billing_date' keys,
        or None if subscription not found or does not belong to customer.
    """
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None

    account_id = customer.get("account_id")
    if account_id and str(subscription.subscriber_id) != str(account_id):
        return None

    current_offer = None
    if subscription.offer_id:
        current_offer = db.get(CatalogOffer, subscription.offer_id)

    next_billing_date = _resolve_next_billing_date(db, subscription)

    return {
        "subscription": subscription,
        "current_offer": current_offer,
        "next_billing_date": next_billing_date,
    }


def get_service_orders_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get service orders page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional service order status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'service_orders', 'status', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    empty_result: dict[str, Any] = {
        "service_orders": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str and not subscription_id_str:
        return empty_result

    service_orders = provisioning_service.service_orders.list(
        db=db,
        subscriber_id=account_id_str,
        subscription_id=subscription_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = select(func.count(ServiceOrder.id))
    if account_id_str:
        stmt = stmt.where(
            ServiceOrder.subscriber_id == coerce_uuid(account_id_str)
        )
    if subscription_id_str:
        stmt = stmt.where(
            ServiceOrder.subscription_id == coerce_uuid(subscription_id_str)
        )
    if status:
        stmt = stmt.where(
            ServiceOrder.status
            == _validate_enum(status, ServiceOrderStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "service_orders": service_orders,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_service_order_detail(
    db: Session,
    customer: dict,
    service_order_id: str,
) -> dict | None:
    """Get service order detail data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        service_order_id: Service order UUID string

    Returns:
        Dict with 'service_order', 'appointments', 'provisioning_tasks' keys,
        or None if service order not found or does not belong to customer.
    """
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    service_order = provisioning_service.service_orders.get(db=db, entity_id=service_order_id)
    if not service_order:
        return None

    # Verify the service order belongs to the customer
    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    appointments = provisioning_service.install_appointments.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="scheduled_start",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    provisioning_tasks = provisioning_service.provisioning_tasks.list(
        db=db,
        service_order_id=service_order_id,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )

    return {
        "service_order": service_order,
        "appointments": appointments,
        "provisioning_tasks": provisioning_tasks,
    }


def get_installation_detail(
    db: Session,
    customer: dict,
    appointment_id: str,
) -> dict | None:
    """Get installation appointment detail data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        appointment_id: Appointment UUID string

    Returns:
        Dict with 'appointment' and 'service_order' keys,
        or None if not found or does not belong to customer.
    """
    account_id = customer.get("account_id")
    subscription_id = customer.get("subscription_id")
    account_id_str = str(account_id) if account_id else None
    subscription_id_str = str(subscription_id) if subscription_id else None

    appointment = provisioning_service.install_appointments.get(db=db, entity_id=appointment_id)
    if not appointment:
        return None

    service_order = provisioning_service.service_orders.get(db=db, entity_id=str(appointment.service_order_id))
    if not service_order:
        return None

    so_subscriber = str(getattr(service_order, "subscriber_id", ""))
    so_subscription = str(getattr(service_order, "subscription_id", ""))
    if (account_id_str and so_subscriber != account_id_str) or (
        subscription_id_str and so_subscription != subscription_id_str
    ):
        return None

    return {
        "appointment": appointment,
        "service_order": service_order,
    }


def get_change_plan_page(
    db: Session,
    customer: dict,
    subscription_id: str,
) -> dict | None:
    """Get change plan page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        subscription_id: Subscription UUID string

    Returns:
        Dict with 'subscription', 'current_offer', 'available_offers',
        'next_billing_date' keys, or None if not found / not authorized.
    """
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
    """Submit a plan change request.

    Args:
        db: Database session
        customer: Customer session dict
        subscription_id: Subscription UUID string
        offer_id: New offer ID string
        effective_date: Effective date string in YYYY-MM-DD format
        notes: Optional notes

    Returns:
        Dict with 'success' True on success.

    Raises:
        ValueError: If effective_date is in the past or otherwise invalid.
        Exception: If the underlying change service raises.
    """
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
    """Get context data for re-rendering the change plan form after an error.

    Args:
        db: Database session
        subscription_id: Subscription UUID string

    Returns:
        Dict with 'subscription', 'current_offer', 'available_offers',
        'next_billing_date' keys.
    """
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
    """Get change requests page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional change request status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'change_requests', 'status', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    from app.services import subscription_changes as change_service

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, Any] = {
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

    stmt = (
        select(func.count(SubscriptionChangeRequest.id))
        .where(SubscriptionChangeRequest.is_active.is_(True))
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


def get_payment_arrangements_page(
    db: Session,
    customer: dict,
    status: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict:
    """Get payment arrangements page data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        status: Optional arrangement status filter
        page: Page number
        per_page: Items per page

    Returns:
        Dict with 'arrangements', 'status', 'page', 'per_page', 'total',
        'total_pages' keys.
    """
    from app.services import payment_arrangements as arrangement_service

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    empty_result: dict[str, Any] = {
        "arrangements": [],
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": 0,
        "total_pages": 1,
    }
    if not account_id_str:
        return empty_result

    arrangements = arrangement_service.payment_arrangements.list(
        db=db,
        account_id=account_id_str,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )

    stmt = (
        select(func.count(PaymentArrangement.id))
        .where(PaymentArrangement.is_active.is_(True))
    )
    if account_id_str:
        stmt = stmt.where(
            PaymentArrangement.subscriber_id == coerce_uuid(account_id_str)
        )
    if status:
        stmt = stmt.where(
            PaymentArrangement.status
            == _validate_enum(status, ArrangementStatus, "status")
        )
    total = db.scalar(stmt) or 0

    return {
        "arrangements": arrangements,
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": _compute_total_pages(total, per_page),
    }


def get_new_arrangement_page(
    db: Session,
    customer: dict,
    invoice_id: str | None = None,
) -> dict:
    """Get data for the new payment arrangement form.

    Args:
        db: Database session
        customer: Customer session dict
        invoice_id: Optional pre-selected invoice ID

    Returns:
        Dict with 'invoices', 'selected_invoice', 'outstanding_balance' keys.
    """
    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None

    invoices: list[Any] = []
    outstanding_balance: int | float = 0
    if account_id_str:
        balance_data = get_outstanding_balance(db, account_id_str)
        invoices = balance_data["invoices"]
        outstanding_balance = balance_data["outstanding_balance"]

    selected_invoice = None
    if invoice_id:
        selected_invoice = billing_service.invoices.get(
            db=db, invoice_id=invoice_id
        )

    return {
        "invoices": invoices,
        "selected_invoice": selected_invoice,
        "outstanding_balance": outstanding_balance,
    }


def submit_payment_arrangement(
    db: Session,
    customer: dict,
    total_amount: str,
    installments: int,
    frequency: str,
    start_date: str,
    invoice_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Submit a payment arrangement request.

    Args:
        db: Database session
        customer: Customer session dict
        total_amount: Total amount as string
        installments: Number of installments
        frequency: Payment frequency
        start_date: Start date in YYYY-MM-DD format
        invoice_id: Optional linked invoice ID
        notes: Optional notes

    Returns:
        Dict with 'success' True on success.

    Raises:
        ValueError: If inputs are invalid.
        Exception: If the underlying arrangement service raises.
    """
    from app.services import payment_arrangements as arrangement_service

    account_id = customer.get("account_id")
    account_id_str = str(account_id) if account_id else None
    subscriber_id = customer.get("subscriber_id")
    subscriber = db.get(Subscriber, subscriber_id) if subscriber_id else None

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    amount = Decimal(total_amount.replace(",", ""))

    if not account_id_str:
        raise ValueError("account_id is required to create a payment arrangement")

    arrangement_service.payment_arrangements.create(
        db=db,
        account_id=account_id_str,
        total_amount=amount,
        installments=installments,
        frequency=frequency,
        start_date=start,
        invoice_id=invoice_id if invoice_id else None,
        requested_by_person_id=str(subscriber.id) if subscriber else None,
        notes=notes,
    )
    return {"success": True}


def get_arrangement_error_context(
    db: Session,
    account_id_str: str | None,
) -> dict:
    """Get context data for re-rendering the arrangement form after an error.

    Args:
        db: Database session
        account_id_str: Account ID string or None

    Returns:
        Dict with 'invoices' and 'outstanding_balance' keys.
    """
    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id_str,
        status="overdue",
        is_active=True,
        order_by="due_at",
        order_dir="asc",
        limit=50,
        offset=0,
    )
    outstanding_balance = sum(inv.balance_due or 0 for inv in invoices)
    return {"invoices": invoices, "outstanding_balance": outstanding_balance}


def get_payment_arrangement_detail(
    db: Session,
    customer: dict,
    arrangement_id: str,
) -> dict | None:
    """Get payment arrangement detail data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        arrangement_id: Arrangement UUID string

    Returns:
        Dict with 'arrangement' and 'installments' keys,
        or None if not found or does not belong to customer.
    """
    from app.services import payment_arrangements as arrangement_service

    account_id = customer.get("account_id")

    try:
        arrangement = arrangement_service.payment_arrangements.get(
            db=db, arrangement_id=arrangement_id
        )
    except Exception:
        return None

    if not arrangement:
        return None

    if account_id and str(arrangement.subscriber_id) != str(account_id):
        return None

    installments = arrangement_service.installments.list(
        db=db,
        arrangement_id=arrangement_id,
        status=None,
        order_by="installment_number",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    return {
        "arrangement": arrangement,
        "installments": installments,
    }


def get_invoice_detail(
    db: Session,
    customer: dict,
    invoice_id: str,
) -> dict | None:
    """Get invoice detail data for the customer portal.

    Args:
        db: Database session
        customer: Customer session dict
        invoice_id: Invoice UUID string

    Returns:
        Dict with 'invoice', 'billing_name', 'billing_email' keys,
        or None if not found or does not belong to customer.
    """
    allowed_account_ids = get_allowed_account_ids(customer, db)

    invoice = billing_service.invoices.get(db=db, invoice_id=invoice_id)
    if not invoice or (
        allowed_account_ids
        and str(getattr(invoice, "account_id", "")) not in allowed_account_ids
    ):
        return None

    billing_contact = get_invoice_billing_contact(db, invoice, customer)

    return {
        "invoice": invoice,
        "billing_name": billing_contact["billing_name"],
        "billing_email": billing_contact["billing_email"],
    }
