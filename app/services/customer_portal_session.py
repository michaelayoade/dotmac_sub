"""Session and identity helpers for customer portal."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.catalog import AccessCredential, Subscription
from app.models.domain_settings import SettingDomain
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.session_store import (
    delete_session,
    get_session_revocation_epoch,
    load_session,
    set_session_revocation_epoch,
    store_session,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "customer_session"
# Default values for fallback when db is not available
_DEFAULT_SESSION_TTL = 86400  # 24 hours
_DEFAULT_REMEMBER_TTL = 2592000  # 30 days
_DEFAULT_ABSOLUTE_TTL = 2592000  # 30 days

_CUSTOMER_SESSIONS: dict[str, dict] = {}
_CUSTOMER_SESSION_EPOCHS: dict[str, str] = {}
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
    is_impersonation: bool = False,
    read_only: bool = False,
    remember: bool = False,
    db: Session | None = None,
) -> str:
    """Create a new customer session and return the session token.

    ``read_only`` marks a viewing-only session (reseller "view as customer"):
    state-changing requests are blocked by the portal write-guard and write
    controls are hidden in the UI. Admin "Login as Customer" is not read-only.
    """
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    session_payload = {
        "username": username,
        "account_id": str(account_id) if account_id else None,
        "subscriber_id": str(subscriber_id) if subscriber_id else None,
        "subscription_id": str(subscription_id) if subscription_id else None,
        "return_to": return_to,
        "is_impersonation": is_impersonation,
        "read_only": read_only,
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

    if _customer_session_revoked(session):
        invalidate_customer_session(session_token)
        return None

    return session


def _customer_session_revoked(session: dict) -> bool:
    """True when a revoke-all epoch postdates this session's creation."""
    subscriber_id = session.get("subscriber_id")
    if not subscriber_id:
        return False
    epoch = get_session_revocation_epoch(
        _CUSTOMER_SESSION_PREFIX, str(subscriber_id), _CUSTOMER_SESSION_EPOCHS
    )
    if not epoch:
        return False
    created_raw = session.get("created_at")
    if not created_raw:
        return True
    try:
        created = datetime.fromisoformat(str(created_raw))
        epoch_at = datetime.fromisoformat(epoch)
    except ValueError:
        return True
    return created <= epoch_at


def revoke_customer_sessions_for_subscriber(
    subscriber_id: object, db: Session | None = None
) -> None:
    """Invalidate every existing customer portal session for a subscriber."""
    ttl = max(
        _session_ttl_seconds(remember=True, db=db),
        _session_ttl_seconds(remember=False, db=db),
        _absolute_ttl_seconds(db),
    )
    set_session_revocation_epoch(
        _CUSTOMER_SESSION_PREFIX, str(subscriber_id), ttl, _CUSTOMER_SESSION_EPOCHS
    )


def refresh_customer_session(
    session_token: str, db: Session | None = None
) -> dict | None:
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    if not session:
        return None

    now = datetime.now(UTC)
    expires_at = datetime.fromisoformat(session["expires_at"])
    if now > expires_at:
        invalidate_customer_session(session_token)
        return None

    if _customer_session_revoked(session):
        invalidate_customer_session(session_token)
        return None

    ttl_seconds = _session_ttl_seconds(session.get("remember", False), db)
    new_expires_at = now + timedelta(seconds=ttl_seconds)
    # Sliding refresh is capped at an absolute lifetime from creation so a
    # keepalive tab can't extend a session forever.
    created_raw = session.get("created_at")
    if created_raw:
        try:
            created = datetime.fromisoformat(str(created_raw))
        except ValueError:
            created = None
        if created:
            absolute_limit = created + timedelta(seconds=_absolute_ttl_seconds(db))
            if now >= absolute_limit:
                invalidate_customer_session(session_token)
                return None
            new_expires_at = min(new_expires_at, absolute_limit)
    session["expires_at"] = new_expires_at.isoformat()
    store_session(
        _CUSTOMER_SESSION_PREFIX,
        session_token,
        session,
        max(1, int((new_expires_at - now).total_seconds())),
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
    needs_identity_lookup = bool(
        username
        and (
            not session.get("account_id")
            or not session.get("subscriber_id")
            or not session.get("subscription_id")
        )
    )
    if needs_identity_lookup:
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


def _absolute_ttl_seconds(db: Session | None = None) -> int:
    """Absolute cap on a session's lifetime regardless of sliding refreshes."""
    ttl = (
        resolve_value(db, SettingDomain.auth, "customer_session_absolute_ttl_seconds")
        if db
        else None
    )
    return _parse_setting_int(ttl, _DEFAULT_ABSOLUTE_TTL)


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
        if subscriber.category == SubscriberCategory.business:
            name = (
                subscriber.company_name
                or subscriber.display_name
                or f"{subscriber.first_name} {subscriber.last_name}".strip()
                or name
            )
        else:
            name = (
                subscriber.display_name
                or f"{subscriber.first_name} {subscriber.last_name}".strip()
                or name
            )
        email = subscriber.email or email
    if not email and session.get("username"):
        email = session.get("username")

    initials = "".join([part[:1] for part in name.split() if part]).upper()[:2] or "CU"
    return {"name": name, "email": email or "", "initials": initials}
