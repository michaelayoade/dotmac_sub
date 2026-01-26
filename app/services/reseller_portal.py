import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.auth import Session as AuthSession, SessionStatus
from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Reseller, ResellerUser, Subscriber, SubscriberAccount
import app.services.auth_flow as auth_flow_service
from app.services import customer_portal
from app.services import catalog as catalog_service
from app.services.common import coerce_uuid
from app.services.settings_spec import resolve_value

SESSION_COOKIE_NAME = "reseller_session"
# Default values for fallback
_DEFAULT_SESSION_TTL = 86400  # 24 hours
_DEFAULT_REMEMBER_TTL = 2592000  # 30 days

# Simple in-memory session store (in production, use Redis or database)
_RESELLER_SESSIONS: dict[str, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _initials(subscriber: Subscriber) -> str:
    first = (subscriber.first_name or "").strip()[:1]
    last = (subscriber.last_name or "").strip()[:1]
    initials = f"{first}{last}".upper()
    return initials or "RS"


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if not subscriber:
        return "Account"
    if subscriber.subscriber:
        # Check for organization first (B2B case)
        if subscriber.subscriber.organization:
            organization = subscriber.subscriber.organization
            return organization.legal_name or organization.name or "Customer"
        # Individual subscriber
        first = subscriber.subscriber.first_name or ""
        last = subscriber.subscriber.last_name or ""
        display = f"{first} {last}".strip()
        return display or subscriber.subscriber.display_name or "Customer"
    return "Customer"


def _get_reseller_user(db: Session, subscriber_id: str) -> ResellerUser | None:
    return (
        db.query(ResellerUser)
        .filter(ResellerUser.subscriber_id == coerce_uuid(subscriber_id))
        .filter(ResellerUser.is_active.is_(True))
        .order_by(ResellerUser.created_at.desc())
        .first()
    )


def _create_session(
    username: str,
    subscriber_id: str,
    reseller_id: str,
    remember: bool,
    db: Session | None = None,
) -> str:
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    _RESELLER_SESSIONS[session_token] = {
        "username": username,
        "subscriber_id": subscriber_id,
        "reseller_id": reseller_id,
        "remember": remember,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    return session_token


def _get_session(session_token: str) -> dict | None:
    session = _RESELLER_SESSIONS.get(session_token)
    if not session:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    if _now() > expires_at:
        del _RESELLER_SESSIONS[session_token]
        return None
    return session


def invalidate_session(session_token: str) -> None:
    _RESELLER_SESSIONS.pop(session_token, None)


def login(db: Session, username: str, password: str, request: Request, remember: bool) -> dict:
    result = auth_flow_service.auth_flow.login(db, username, password, request, None)
    if result.get("mfa_required"):
        return {"mfa_required": True, "mfa_token": result.get("mfa_token")}
    access_token = result.get("access_token")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return _session_from_access_token(db, access_token, username, remember)


def verify_mfa(db: Session, mfa_token: str, code: str, request: Request, remember: bool) -> dict:
    result = auth_flow_service.auth_flow.mfa_verify(db, mfa_token, code, request)
    access_token = result.get("access_token")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid verification code")
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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    auth_session = db.get(AuthSession, coerce_uuid(session_id))
    if not auth_session or auth_session.status != SessionStatus.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    if auth_session.expires_at and auth_session.expires_at <= _now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    reseller_user = _get_reseller_user(db, str(subscriber_id))
    if not reseller_user:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Reseller access required")

    subscriber = db.get(Subscriber, reseller_user.subscriber_id)
    if not subscriber:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found")

    session_token = _create_session(
        username=username or subscriber.email,
        subscriber_id=str(subscriber.id),
        reseller_id=str(reseller_user.reseller_id),
        remember=remember,
        db=db,
    )
    return {"session_token": session_token, "reseller_id": str(reseller_user.reseller_id)}


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
        "name": subscriber.display_name or f"{subscriber.first_name} {subscriber.last_name}".strip(),
        "email": subscriber.email,
        "initials": _initials(subscriber),
    }
    return {
        "session": session,
        "current_user": current_user,
        "subscriber": subscriber,
        "reseller": reseller,
        "reseller_user": reseller_user,
    }


def refresh_session(session_token: str | None, db: Session | None = None) -> dict | None:
    if not session_token:
        return None
    session = _get_session(session_token)
    if not session:
        return None
    ttl_seconds = _session_ttl_seconds(session.get("remember", False), db)
    session["expires_at"] = (_now() + timedelta(seconds=ttl_seconds)).isoformat()
    return session


def _session_ttl_seconds(remember: bool, db: Session | None = None) -> int:
    """Get session TTL in seconds, using configurable settings when db is available."""
    if remember:
        ttl = resolve_value(db, SettingDomain.auth, "reseller_remember_ttl_seconds") if db else None
        return ttl if ttl is not None else _DEFAULT_REMEMBER_TTL
    else:
        ttl = resolve_value(db, SettingDomain.auth, "reseller_session_ttl_seconds") if db else None
        return ttl if ttl is not None else _DEFAULT_SESSION_TTL


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
) -> list[dict]:
    accounts = (
        db.query(SubscriberAccount)
        .options(
            selectinload(SubscriberAccount.subscriber)
            .selectinload(Subscriber.subscriber)
            .selectinload(Subscriber.organization),
        )
        .filter(SubscriberAccount.reseller_id == coerce_uuid(reseller_id))
        .order_by(SubscriberAccount.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
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
                "subscriber_name": _subscriber_label(account.subscriber),
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
        db.query(func.count(SubscriberAccount.id))
        .filter(SubscriberAccount.reseller_id == coerce_uuid(reseller_id))
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
        .join(SubscriberAccount, Invoice.account_id == SubscriberAccount.id)
        .filter(SubscriberAccount.reseller_id == coerce_uuid(reseller_id))
        .filter(Invoice.status.in_(open_statuses))
        .first()
    )
    open_balance = balance_row.open_balance if balance_row else 0
    open_invoices = balance_row.open_invoices if balance_row else 0

    return {
        "accounts": accounts,
        "totals": {
            "accounts": total_accounts,
            "open_balance": open_balance,
            "open_invoices": open_invoices,
        },
    }


def create_customer_imsubscriberation_session(
    db: Session,
    reseller_id: str,
    account_id: str,
    return_to: str,
) -> str:
    account = db.get(SubscriberAccount, coerce_uuid(account_id))
    if not account or str(account.reseller_id) != str(reseller_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber account not found")

    selected_subscription_id = None
    active_subs = catalog_service.subscriptions.list(
        db=db,
        account_id=str(account.id),
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
            account_id=str(account.id),
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
        subscriber_id=account.subscriber_id,
        subscription_id=selected_subscription_id,
        return_to=return_to,
    )
    return session_token
