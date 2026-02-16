import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential, CatalogOffer, Subscription
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import AccountStatus, Subscriber, Organization
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import subscriber as subscriber_service
from app.services import provisioning as provisioning_service
from app.services.settings_spec import resolve_value

SESSION_COOKIE_NAME = "customer_session"
# Default values for fallback when db is not available
_DEFAULT_SESSION_TTL = 86400  # 24 hours
_DEFAULT_REMEMBER_TTL = 2592000  # 30 days

# Simple in-memory session store (in production, use Redis or database)
_CUSTOMER_SESSIONS: dict[str, dict] = {}


def create_customer_session(
    username: str,
    account_id: Optional[UUID],
    subscriber_id: Optional[UUID],
    subscription_id: Optional[UUID] = None,
    return_to: Optional[str] = None,
    remember: bool = False,
    db: Session | None = None,
) -> str:
    """Create a new customer session and return the session token."""
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    _CUSTOMER_SESSIONS[session_token] = {
        "username": username,
        "account_id": str(account_id) if account_id else None,
        "subscriber_id": str(subscriber_id) if subscriber_id else None,
        "subscription_id": str(subscription_id) if subscription_id else None,
        "return_to": return_to,
        "remember": remember,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    return session_token


def get_customer_session(session_token: str) -> Optional[dict]:
    """Get customer session data from token."""
    session = _CUSTOMER_SESSIONS.get(session_token)
    if not session:
        return None

    # Check expiration
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        del _CUSTOMER_SESSIONS[session_token]
        return None

    return session


def refresh_customer_session(session_token: str, db: Session | None = None) -> Optional[dict]:
    session = _CUSTOMER_SESSIONS.get(session_token)
    if not session:
        return None

    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(timezone.utc) > expires_at:
        del _CUSTOMER_SESSIONS[session_token]
        return None

    ttl_seconds = _session_ttl_seconds(session.get("remember", False), db)
    session["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    return session


def invalidate_customer_session(session_token: str) -> None:
    """Invalidate a customer session."""
    _CUSTOMER_SESSIONS.pop(session_token, None)


def get_current_customer(session_token: str | None, db: Session) -> Optional[dict]:
    """Resolve a customer session token into a hydrated session dict."""
    if not session_token:
        return None

    session = get_customer_session(session_token)
    if not session:
        return None

    # Enrich session with user data
    username = session.get("username")
    if username:
        radius_user = (
            db.query(RadiusUser)
            .filter(RadiusUser.username == username)
            .filter(RadiusUser.is_active.is_(True))
            .first()
        )
        if radius_user:
            session["radius_user_id"] = str(radius_user.id)
            if radius_user.account_id:
                session["account_id"] = str(radius_user.account_id)
            if radius_user.subscription_id:
                session["subscription_id"] = str(radius_user.subscription_id)
        else:
            credential = (
                db.query(AccessCredential)
                .filter(AccessCredential.username == username)
                .filter(AccessCredential.is_active.is_(True))
                .first()
            )
            if credential:
                session["account_id"] = str(credential.subscriber_id)
                session["subscriber_id"] = str(credential.subscriber_id)
            else:
                local_credential = (
                    db.query(UserCredential)
                    .filter(UserCredential.username == username)
                    .filter(UserCredential.provider == AuthProvider.local)
                    .filter(UserCredential.is_active.is_(True))
                    .first()
                )
                if local_credential:
                    subscriber = (
                        db.query(Subscriber)
                        .filter(Subscriber.id == local_credential.subscriber_id)
                        .filter(Subscriber.is_active.is_(True))
                        .first()
                    )
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
        ttl = resolve_value(db, SettingDomain.auth, "customer_remember_ttl_seconds") if db else None
        return ttl if ttl is not None else _DEFAULT_REMEMBER_TTL
    else:
        ttl = resolve_value(db, SettingDomain.auth, "customer_session_ttl_seconds") if db else None
        return ttl if ttl is not None else _DEFAULT_SESSION_TTL


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


def _get_status_value(value) -> str:
    if value is None:
        return ""
    return value.value if hasattr(value, "value") else str(value)


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
        next_bill_date = datetime.now(timezone.utc) + timedelta(days=30)

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
                if _get_status_value(price.price_type) == "recurring" and price.is_active
            ]
        monthly_cost = float(recurring_prices[0].amount) if recurring_prices else 0.0
        services.append(
            SimpleNamespace(
                name=offer.name if offer else "Service",
                speed=speed,
                address=address,
                status=_get_status_value(subscription.status) or "pending",
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

    if account and account.subscriber:
        sub = account.subscriber
        billing_name = sub.display_name or f"{sub.first_name} {sub.last_name}".strip()
        billing_email = sub.email
        if sub.organization_id:
            organization = db.get(Organization, sub.organization_id)
            if organization:
                billing_name = organization.name
            primary_email = None
            if account.account_roles:
                primary_role = next(
                    (role for role in account.account_roles if role.is_primary),
                    account.account_roles[0],
                )
                if primary_role and primary_role.subscriber:
                    primary_email = primary_role.subscriber.email
            billing_email = primary_email

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

    # Get service orders for the customer
    service_orders = provisioning_service.service_orders.list(
        db=db,
        account_id=account_id_str,
        subscription_id=subscription_id_str,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    order_ids = {str(order.id) for order in service_orders}

    if not order_ids:
        return {"appointments": [], "total": 0, "total_pages": 1}

    # Get appointments for those service orders
    all_appointments = provisioning_service.install_appointments.list(
        db=db,
        service_order_id=None,
        status=status if status else None,
        order_by="scheduled_start",
        order_dir="desc",
        limit=10000,
        offset=0,
    )
    scoped_appointments = [
        appt for appt in all_appointments if str(appt.service_order_id) in order_ids
    ]
    total = len(scoped_appointments)
    total_pages = (total + per_page - 1) // per_page if total else 1
    start = (page - 1) * per_page
    appointments = scoped_appointments[start:start + per_page]

    return {"appointments": appointments, "total": total, "total_pages": total_pages}


def get_available_portal_offers(db: Session) -> list:
    """Get catalog offers available on the customer portal.

    Args:
        db: Database session

    Returns:
        List of CatalogOffer instances
    """
    return (
        db.query(CatalogOffer)
        .filter(CatalogOffer.is_active.is_(True))
        .filter(CatalogOffer.show_on_customer_portal.is_(True))
        .order_by(CatalogOffer.price.asc())
        .all()
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
