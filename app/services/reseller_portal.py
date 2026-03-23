import logging
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, selectinload

import app.services.auth_flow as auth_flow_service
from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from app.models.catalog import CatalogOffer, Subscription
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Reseller, ResellerUser, Subscriber
from app.services import catalog as catalog_service
from app.services import customer_portal
from app.services.common import coerce_uuid
from app.services.session_store import delete_session, load_session, store_session
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _emit_reseller_event(db: Session, event_name: str, payload: dict) -> None:
    """Emit a reseller event to the event system (non-blocking)."""
    try:
        from app.services.events import emit_event
        from app.services.events.types import EventType

        event_type = getattr(EventType, event_name, None)
        if event_type:
            emit_event(db, event_type, payload, actor="reseller")
    except Exception as e:
        logger.warning("Failed to emit reseller event %s: %s", event_name, e)


SESSION_COOKIE_NAME = "reseller_session"
# Default values for fallback
_DEFAULT_SESSION_TTL = 86400  # 24 hours
_DEFAULT_REMEMBER_TTL = 2592000  # 30 days

_RESELLER_SESSIONS: dict[str, dict] = {}
_RESELLER_SESSION_PREFIX = "session:reseller_portal"


def _now() -> datetime:
    return datetime.now(UTC)


def _initials(subscriber: Subscriber) -> str:
    first = (subscriber.first_name or "").strip()[:1]
    last = (subscriber.last_name or "").strip()[:1]
    initials = f"{first}{last}".upper()
    return initials or "RS"


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if not subscriber:
        return "Account"
    # Backwards-compat: older code treats SubscriberAccount as having a `.person`
    # relationship (and optionally organization via that person).
    person = getattr(subscriber, "person", None)
    base = person or subscriber

    def _clean_str(value: object | None) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    organization = getattr(base, "organization", None)
    if organization:
        legal_name = _clean_str(getattr(organization, "legal_name", None))
        name = _clean_str(getattr(organization, "name", None))
        if legal_name:
            return legal_name
        if name:
            return name
    first = _clean_str(getattr(base, "first_name", None))
    last = _clean_str(getattr(base, "last_name", None))
    display = f"{first} {last}".strip()
    display_name = _clean_str(getattr(base, "display_name", None))
    return display or display_name or "Customer"


def _get_reseller_user(db: Session, subscriber_id: str) -> ResellerUser | None:
    # Preferred path for schemas with dedicated reseller user link table.
    try:
        return (
            db.query(ResellerUser)
            .filter(ResellerUser.subscriber_id == coerce_uuid(subscriber_id))
            .filter(ResellerUser.is_active.is_(True))
            .order_by(ResellerUser.created_at.desc())
            .first()
        )
    except ProgrammingError:
        # Compatibility path for schemas without reseller_users* table.
        db.rollback()

    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    if not subscriber or not subscriber.is_active or not subscriber.reseller_id:
        return None
    user_type = getattr(subscriber, "user_type", None)
    if getattr(user_type, "value", user_type) != "reseller":
        return None
    return SimpleNamespace(
        id=subscriber.id,
        subscriber_id=subscriber.id,
        person_id=subscriber.id,
        reseller_id=subscriber.reseller_id,
        is_active=True,
        created_at=subscriber.created_at,
    )


def _create_session(
    username: str,
    reseller_id: str,
    remember: bool,
    subscriber_id: str | None = None,
    db: Session | None = None,
    person_id: str | None = None,
) -> str:
    if not subscriber_id:
        subscriber_id = person_id
    if not subscriber_id:
        raise ValueError("subscriber_id/person_id is required")
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    session_payload = {
        "username": username,
        "subscriber_id": subscriber_id,
        # Backwards-compat: older tests/callers use "person_id".
        "person_id": subscriber_id,
        "reseller_id": reseller_id,
        "remember": remember,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    store_session(
        _RESELLER_SESSION_PREFIX,
        session_token,
        session_payload,
        ttl_seconds,
        _RESELLER_SESSIONS,
    )
    return session_token


def _get_session(session_token: str) -> dict | None:
    session = load_session(_RESELLER_SESSION_PREFIX, session_token, _RESELLER_SESSIONS)
    if not session:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    if _now() > expires_at:
        invalidate_session(session_token)
        return None
    return session


def invalidate_session(session_token: str, db: Session | None = None) -> None:
    # Read raw session without going through _get_session (which calls invalidate on expiry)
    session = load_session(_RESELLER_SESSION_PREFIX, session_token, _RESELLER_SESSIONS)
    delete_session(_RESELLER_SESSION_PREFIX, session_token, _RESELLER_SESSIONS)
    if db and session:
        _emit_reseller_event(
            db,
            "reseller_logout",
            {
                "reseller_id": session.get("reseller_id", ""),
            },
        )


def login(
    db: Session, username: str, password: str, request: Request, remember: bool
) -> dict:
    result = auth_flow_service.auth_flow.login(db, username, password, request, None)
    if result.get("mfa_required"):
        return {"mfa_required": True, "mfa_token": result.get("mfa_token")}
    access_token = result.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    return _session_from_access_token(db, access_token, username, remember)


def verify_mfa(
    db: Session, mfa_token: str, code: str, request: Request, remember: bool
) -> dict:
    result = auth_flow_service.auth_flow.mfa_verify(db, mfa_token, code, request)
    access_token = result.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code"
        )
    return _session_from_access_token(db, access_token, None, remember)


def _session_from_access_token(
    db: Session,
    access_token: str,
    username: str | None,
    remember: bool,
) -> dict:
    payload = auth_flow_service.decode_access_token(db, access_token)
    subscriber_id = payload.get("sub")
    session_id = payload.get("session_id")
    if not subscriber_id or not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        )

    auth_session = db.get(AuthSession, coerce_uuid(session_id))
    if not auth_session or auth_session.status != SessionStatus.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        )
    if auth_session.expires_at and auth_session.expires_at <= _now():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
        )

    reseller_user = _get_reseller_user(db, str(subscriber_id))
    if not reseller_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Reseller access required"
        )

    subscriber = db.get(Subscriber, reseller_user.subscriber_id)
    if not subscriber:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found"
        )

    session_token = _create_session(
        username=username or subscriber.email,
        subscriber_id=str(subscriber.id),
        reseller_id=str(reseller_user.reseller_id),
        remember=remember,
        db=db,
    )
    _emit_reseller_event(
        db,
        "reseller_login",
        {
            "reseller_id": str(reseller_user.reseller_id),
            "subscriber_id": str(subscriber.id),
        },
    )
    return {
        "session_token": session_token,
        "reseller_id": str(reseller_user.reseller_id),
    }


def get_context(db: Session, session_token: str | None) -> dict | None:
    session = _get_session(session_token or "")
    if not session:
        return None

    subscriber = db.get(Subscriber, coerce_uuid(session["subscriber_id"]))
    reseller = db.get(Reseller, coerce_uuid(session["reseller_id"]))
    if not subscriber or not reseller:
        return None

    reseller_user = _get_reseller_user(db, str(subscriber.id))
    if not reseller_user:
        return None

    current_user = {
        "name": subscriber.display_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip(),
        "email": subscriber.email,
        "initials": _initials(subscriber),
    }
    return {
        "session": session,
        "current_user": current_user,
        # Backwards-compat: older callers/tests expect `person`.
        "person": subscriber,
        "subscriber": subscriber,
        "reseller": reseller,
        "reseller_user": reseller_user,
    }


def refresh_session(
    session_token: str | None, db: Session | None = None
) -> dict | None:
    if not session_token:
        return None
    session = _get_session(session_token)
    if not session:
        return None
    ttl_seconds = _session_ttl_seconds(session.get("remember", False), db)
    session["expires_at"] = (_now() + timedelta(seconds=ttl_seconds)).isoformat()
    store_session(
        _RESELLER_SESSION_PREFIX,
        session_token,
        session,
        ttl_seconds,
        _RESELLER_SESSIONS,
    )
    return session


def _session_ttl_seconds(remember: bool, db: Session | None = None) -> int:
    """Get session TTL in seconds, using configurable settings when db is available."""
    if remember:
        ttl = (
            resolve_value(db, SettingDomain.auth, "reseller_remember_ttl_seconds")
            if db
            else None
        )
        if ttl is None:
            return _DEFAULT_REMEMBER_TTL
        try:
            return int(str(ttl))
        except (TypeError, ValueError):
            return _DEFAULT_REMEMBER_TTL
    else:
        ttl = (
            resolve_value(db, SettingDomain.auth, "reseller_session_ttl_seconds")
            if db
            else None
        )
        if ttl is None:
            return _DEFAULT_SESSION_TTL
        try:
            return int(str(ttl))
        except (TypeError, ValueError):
            return _DEFAULT_SESSION_TTL


def get_session_max_age(db: Session | None = None) -> int:
    """Get the session max age for non-remember sessions."""
    return _session_ttl_seconds(remember=False, db=db)


def get_remember_max_age(db: Session | None = None) -> int:
    """Get the session max age for remember-me sessions."""
    return _session_ttl_seconds(remember=True, db=db)


def list_accounts(
    db: Session,
    reseller_id: str,
    limit: int,
    offset: int,
    search: str | None = None,
) -> list[dict]:
    query = (
        db.query(Subscriber)
        .options(selectinload(Subscriber.organization))
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
    )
    if search:
        pattern = f"%{search.strip()}%"
        query = query.filter(
            (Subscriber.first_name.ilike(pattern))
            | (Subscriber.last_name.ilike(pattern))
            | (Subscriber.email.ilike(pattern))
            | (Subscriber.account_number.ilike(pattern))
        )
    accounts = (
        query.order_by(Subscriber.created_at.desc()).limit(limit).offset(offset).all()
    )
    account_ids = [account.id for account in accounts]
    if not account_ids:
        return []

    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    invoice_rows = (
        db.query(
            Invoice.account_id,
            func.coalesce(func.sum(Invoice.balance_due), 0).label("open_balance"),
            func.count(Invoice.id).label("open_invoices"),
        )
        .filter(Invoice.account_id.in_(account_ids))
        .filter(Invoice.status.in_(open_statuses))
        .group_by(Invoice.account_id)
        .all()
    )
    invoice_summary = {
        str(row.account_id): {"balance": row.open_balance, "count": row.open_invoices}
        for row in invoice_rows
    }

    payment_rows = (
        db.query(
            Payment.account_id,
            func.max(Payment.paid_at).label("last_paid_at"),
        )
        .filter(Payment.account_id.in_(account_ids))
        .filter(Payment.status == PaymentStatus.succeeded)
        .group_by(Payment.account_id)
        .all()
    )
    last_payments = {str(row.account_id): row.last_paid_at for row in payment_rows}

    results = []
    for account in accounts:
        summary = invoice_summary.get(str(account.id), {})
        results.append(
            {
                "id": str(account.id),
                "account_number": account.account_number,
                "subscriber_name": _subscriber_label(account),
                "status": account.status.value if account.status else "active",
                "open_balance": summary.get("balance", 0),
                "open_invoices": summary.get("count", 0),
                "last_payment_at": last_payments.get(str(account.id)),
            }
        )
    return results


def get_dashboard_summary(
    db: Session,
    reseller_id: str,
    limit: int,
    offset: int,
) -> dict:
    accounts = list_accounts(db, reseller_id, limit, offset)

    total_accounts = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .scalar()
        or 0
    )
    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    balance_row = (
        db.query(
            func.coalesce(func.sum(Invoice.balance_due), 0).label("open_balance"),
            func.count(Invoice.id).label("open_invoices"),
        )
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .filter(Invoice.status.in_(open_statuses))
        .first()
    )
    open_balance = balance_row.open_balance if balance_row else 0
    open_invoices = balance_row.open_invoices if balance_row else 0

    # Alert data: overdue invoices, new accounts this week, suspended accounts
    overdue_count = (
        db.query(func.count(Invoice.id))
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .filter(Invoice.status == InvoiceStatus.overdue)
        .scalar()
        or 0
    )

    week_ago = datetime.now(UTC) - timedelta(days=7)
    new_this_week = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .filter(Subscriber.created_at >= week_ago)
        .scalar()
        or 0
    )

    from app.models.subscriber import SubscriberStatus

    suspended_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.reseller_id == coerce_uuid(reseller_id))
        .filter(
            Subscriber.status.in_(
                [SubscriberStatus.suspended, SubscriberStatus.blocked]
            )
        )
        .scalar()
        or 0
    )

    alerts = []
    if overdue_count > 0:
        alerts.append(
            {
                "level": "warning",
                "icon": "clock",
                "message": f"{overdue_count} overdue invoice{'s' if overdue_count != 1 else ''} require attention",
                "action_url": "/reseller/accounts",
            }
        )
    if suspended_count > 0:
        alerts.append(
            {
                "level": "danger",
                "icon": "pause",
                "message": f"{suspended_count} account{'s' if suspended_count != 1 else ''} suspended",
                "action_url": "/reseller/accounts",
            }
        )
    if new_this_week > 0:
        alerts.append(
            {
                "level": "info",
                "icon": "user-plus",
                "message": f"{new_this_week} new account{'s' if new_this_week != 1 else ''} this week",
                "action_url": "/reseller/accounts",
            }
        )

    return {
        "accounts": accounts,
        "totals": {
            "accounts": total_accounts,
            "open_balance": open_balance,
            "open_invoices": open_invoices,
        },
        "alerts": alerts,
    }


def get_account_detail(
    db: Session,
    reseller_id: str,
    account_id: str,
) -> dict | None:
    """Get detailed subscriber info with subscriptions, scoped by reseller.

    Returns dict with subscriber details and active subscriptions,
    or None if account not found or not owned by reseller.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account or str(account.reseller_id) != str(coerce_uuid(reseller_id)):
        return None

    # Fetch subscriptions with offer details
    subscriptions = (
        db.query(Subscription)
        .outerjoin(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .filter(Subscription.subscriber_id == account.id)
        .order_by(Subscription.created_at.desc())
        .all()
    )

    sub_list = []
    for sub in subscriptions:
        offer = db.get(CatalogOffer, sub.offer_id) if sub.offer_id else None
        sub_list.append(
            {
                "id": str(sub.id),
                "offer_name": offer.name if offer else "N/A",
                "status": sub.status.value if sub.status else "unknown",
                "start_date": sub.start_at,
                "end_date": getattr(sub, "end_at", None),
                "created_at": sub.created_at,
            }
        )

    # Invoice summary
    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    open_balance = (
        db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
        .filter(Invoice.account_id == account.id, Invoice.status.in_(open_statuses))
        .scalar()
    ) or 0

    return {
        "id": str(account.id),
        "account_number": account.account_number,
        "subscriber_name": _subscriber_label(account),
        "first_name": account.first_name,
        "last_name": account.last_name,
        "email": account.email,
        "phone": account.phone,
        "status": account.status.value if account.status else "active",
        "address_line1": getattr(account, "address_line1", None),
        "address_line2": getattr(account, "address_line2", None),
        "city": getattr(account, "city", None),
        "region": getattr(account, "region", None),
        "created_at": account.created_at,
        "subscriptions": sub_list,
        "open_balance": open_balance,
    }


def list_account_invoices(
    db: Session,
    reseller_id: str,
    account_id: str,
    limit: int = 25,
    offset: int = 0,
) -> list[dict] | None:
    """List invoices for a reseller's subscriber account.

    Returns list of invoice dicts, or None if account not owned by reseller.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account or str(account.reseller_id) != str(coerce_uuid(reseller_id)):
        return None

    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == account.id)
        .order_by(Invoice.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    results = []
    for inv in invoices:
        results.append(
            {
                "id": str(inv.id),
                "invoice_number": getattr(inv, "invoice_number", None),
                "status": inv.status.value if inv.status else "draft",
                "total_amount": getattr(inv, "total", 0),
                "balance_due": inv.balance_due or 0,
                "issued_at": getattr(inv, "issued_at", None),
                "due_date": getattr(inv, "due_at", None),
                "created_at": inv.created_at,
            }
        )
    return results


def get_invoice_detail(
    db: Session,
    reseller_id: str,
    account_id: str,
    invoice_id: str,
) -> dict | None:
    """Get invoice detail with line items and payments, scoped by reseller.

    Returns dict with invoice data, or None if not found/not authorized.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account or str(account.reseller_id) != str(coerce_uuid(reseller_id)):
        return None

    invoice = db.get(Invoice, coerce_uuid(invoice_id))
    if not invoice or str(invoice.account_id) != str(account.id):
        return None

    # Line items
    line_items = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .order_by(InvoiceLine.created_at.asc())
        .all()
    )
    items = []
    for item in line_items:
        items.append(
            {
                "description": getattr(item, "description", ""),
                "quantity": getattr(item, "quantity", 1),
                "unit_price": getattr(item, "unit_price", 0),
                "amount": getattr(item, "amount", 0),
            }
        )

    # Payments via allocations
    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .all()
    )
    payment_list = []
    for alloc in allocations:
        pmt = db.get(Payment, alloc.payment_id) if alloc.payment_id else None
        if pmt:
            payment_list.append(
                {
                    "id": str(pmt.id),
                    "amount": alloc.amount,
                    "status": pmt.status.value if pmt.status else "pending",
                    "paid_at": pmt.paid_at,
                    "method": getattr(pmt, "label", None),
                }
            )

    return {
        "id": str(invoice.id),
        "invoice_number": getattr(invoice, "invoice_number", None),
        "status": invoice.status.value if invoice.status else "draft",
        "total_amount": getattr(invoice, "total", 0),
        "balance_due": invoice.balance_due or 0,
        "issued_at": getattr(invoice, "issued_at", None),
        "due_date": getattr(invoice, "due_at", None),
        "created_at": invoice.created_at,
        "line_items": items,
        "payments": payment_list,
        "subscriber_name": _subscriber_label(account),
        "account_id": str(account.id),
    }


def get_revenue_summary(
    db: Session,
    reseller_id: str,
) -> dict:
    """Get monthly revenue summary for a reseller's accounts.

    Aggregates invoice amounts by month and status for the last 12 months.
    """
    from sqlalchemy import extract

    reseller_uuid = coerce_uuid(reseller_id)

    # Total revenue (all paid invoices)
    total_paid = (
        db.query(func.coalesce(func.sum(Invoice.total), 0))
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(Invoice.status == InvoiceStatus.paid)
        .scalar()
    ) or 0

    # Outstanding balance
    open_statuses = {
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    }
    total_outstanding = (
        db.query(func.coalesce(func.sum(Invoice.balance_due), 0))
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(Invoice.status.in_(open_statuses))
        .scalar()
    ) or 0

    # Monthly breakdown (last 12 months)
    monthly_rows = (
        db.query(
            extract("year", Invoice.created_at).label("year"),
            extract("month", Invoice.created_at).label("month"),
            func.coalesce(func.sum(Invoice.total), 0).label("total"),
            func.count(Invoice.id).label("count"),
        )
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(Invoice.status == InvoiceStatus.paid)
        .group_by("year", "month")
        .order_by(
            extract("year", Invoice.created_at).desc(),
            extract("month", Invoice.created_at).desc(),
        )
        .limit(12)
        .all()
    )

    monthly = []
    for row in reversed(monthly_rows):
        monthly.append(
            {
                "year": int(row.year),
                "month": int(row.month),
                "total": float(row.total),
                "count": int(row.count),
            }
        )

    # Account count
    account_count = (
        db.query(func.count(Subscriber.id))
        .filter(Subscriber.reseller_id == reseller_uuid)
        .scalar()
    ) or 0

    return {
        "total_paid": total_paid,
        "total_outstanding": total_outstanding,
        "account_count": account_count,
        "monthly": monthly,
    }


def create_customer_imsubscriberation_session(
    db: Session,
    reseller_id: str,
    account_id: str,
    return_to: str,
) -> str:
    account = db.get(Subscriber, coerce_uuid(account_id))
    if not account or str(account.reseller_id) != str(reseller_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber account not found"
        )

    selected_subscription_id = None
    active_subs = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=str(account.id),
        offer_id=None,
        status="active",
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )
    if active_subs:
        selected_subscription_id = active_subs[0].id
    else:
        any_subs = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(account.id),
            offer_id=None,
            status=None,
            order_by="created_at",
            order_dir="desc",
            limit=1,
            offset=0,
        )
        if any_subs:
            selected_subscription_id = any_subs[0].id

    session_token = customer_portal.create_customer_session(
        username=f"imsubscriberate:reseller:{reseller_id}:{account.id}",
        account_id=account.id,
        subscriber_id=account.id,
        subscription_id=selected_subscription_id,
        return_to=return_to,
    )
    _emit_reseller_event(
        db,
        "reseller_impersonated",
        {
            "reseller_id": reseller_id,
            "account_id": account_id,
        },
    )
    return session_token


def create_customer_impersonation_session(
    db: Session,
    reseller_id: str,
    account_id: str,
    return_to: str,
) -> str:
    """Backwards-compat wrapper for a historical typo in the function name."""
    return create_customer_imsubscriberation_session(
        db, reseller_id, account_id, return_to
    )
