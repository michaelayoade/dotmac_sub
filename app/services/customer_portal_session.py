"""Session and identity helpers for customer portal."""

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services.session_store import (
    delete_session,
    get_session_revocation_epoch,
    list_sessions_for_principal,
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
_CUSTOMER_SESSION_INDEX: dict[str, set[str]] = {}
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


@dataclass(frozen=True)
class CreateCustomerPortalSession:
    username: str
    account_id: UUID | None
    subscriber_id: UUID | None
    subscription_id: UUID | None = None
    return_to: str | None = None
    is_impersonation: bool = False
    read_only: bool = False
    remember: bool = False
    auth_session_id: UUID | None = None


@dataclass(frozen=True)
class CustomerPortalSessionCreated:
    token: str
    auth_session_id: UUID


@dataclass(frozen=True)
class CustomerSessionStandingQuery:
    auth_session_id: UUID
    subscriber_id: UUID
    account_id: UUID


@dataclass(frozen=True)
class CustomerSessionStanding:
    auth_session_id: UUID
    subscriber_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class CustomerSessionRefreshQuery:
    standing: CustomerSessionStandingQuery
    created_at: datetime
    remember: bool
    now: datetime


@dataclass(frozen=True)
class CustomerSessionRefreshOutcome:
    expires_at: datetime
    ttl_seconds: int


def create_customer_session(
    username: str,
    account_id: UUID | str | None,
    subscriber_id: UUID | str | None,
    subscription_id: UUID | str | None = None,
    return_to: str | None = None,
    is_impersonation: bool = False,
    read_only: bool = False,
    remember: bool = False,
    db: Session | None = None,
    *,
    auth_session_id: UUID | None = None,
    request: Request | None = None,
) -> str:
    """Compatibility adapter from caller identifiers to typed cache issuance."""
    command = CreateCustomerPortalSession(
        username=username,
        account_id=UUID(str(account_id)) if account_id else None,
        subscriber_id=UUID(str(subscriber_id)) if subscriber_id else None,
        subscription_id=UUID(str(subscription_id)) if subscription_id else None,
        return_to=return_to,
        is_impersonation=is_impersonation,
        read_only=read_only,
        remember=remember,
        auth_session_id=auth_session_id,
    )
    return create_customer_portal_session(db=db, command=command, request=request).token


def create_customer_portal_session(
    *,
    db: Session | None,
    command: CreateCustomerPortalSession,
    request: Request | None,
) -> CustomerPortalSessionCreated:
    """Deliver typed identity backed by already committed database standing."""
    username, account_id, subscriber_id = (
        command.username,
        command.account_id,
        command.subscriber_id,
    )
    subscription_id, return_to = command.subscription_id, command.return_to
    is_impersonation, read_only, remember = (
        command.is_impersonation,
        command.read_only,
        command.remember,
    )
    auth_session_id = command.auth_session_id
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    if is_impersonation and auth_session_id is None:
        if db is None or subscriber_id is None:
            raise ValueError("Impersonation requires database session standing")
        from app.services import password_authentication as pa

        staged = pa.establish_customer_impersonation_session(
            db,
            subscriber_id=UUID(str(subscriber_id)),
            request=request or Request({"type": "http", "headers": []}),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        auth_session_id = UUID(staged.session_id)
    if auth_session_id is None:
        raise ValueError("Customer session requires database standing")
    if db is None or account_id is None or subscriber_id is None:
        raise ValueError("Customer delivery requires database identity standing")
    standing = resolve_customer_session_standing(
        db,
        query=CustomerSessionStandingQuery(
            auth_session_id=auth_session_id,
            subscriber_id=subscriber_id,
            account_id=account_id,
        ),
    )
    if standing is None:
        raise RuntimeError("Customer session standing unavailable")
    ttl_seconds = min(
        ttl_seconds,
        max(1, int((standing.expires_at - datetime.now(UTC)).total_seconds())),
    )
    session_payload = {
        "auth_session_id": str(auth_session_id) if auth_session_id else None,
        "username": username,
        "account_id": str(account_id) if account_id else None,
        "subscriber_id": str(subscriber_id) if subscriber_id else None,
        "subscription_id": str(subscription_id) if subscription_id else None,
        "return_to": return_to,
        "is_impersonation": is_impersonation,
        "read_only": read_only,
        "remember": remember,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": standing.expires_at.isoformat(),
    }
    try:
        store_session(
            _CUSTOMER_SESSION_PREFIX,
            session_token,
            session_payload,
            ttl_seconds,
            _CUSTOMER_SESSIONS,
            principal_id=str(subscriber_id) if subscriber_id else None,
            fallback_index=_CUSTOMER_SESSION_INDEX,
        )
    except RuntimeError:
        if is_impersonation and db is not None and auth_session_id and subscriber_id:
            from app.services import password_authentication as pa

            try:
                pa.revoke_undelivered_customer_session(
                    db,
                    session_id=auth_session_id,
                    subscriber_id=UUID(str(subscriber_id)),
                )
            except Exception:
                logger.warning("undelivered_impersonation_revoke_failed", exc_info=True)
        raise
    return CustomerPortalSessionCreated(session_token, auth_session_id)


def _cache_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def get_customer_session(session_token: str) -> dict | None:
    """Get customer session data from token."""
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    if not session:
        return None

    # Check expiration
    expires_at = _cache_timestamp(session.get("expires_at"))
    if expires_at is None or datetime.now(UTC) >= expires_at:
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
    created_raw = session.get("revocation_exempted_at") or session.get("created_at")
    if not created_raw:
        return True
    try:
        created = datetime.fromisoformat(str(created_raw))
        epoch_at = datetime.fromisoformat(epoch)
    except ValueError:
        return True
    return created <= epoch_at


def revoke_customer_sessions_for_subscriber(
    subscriber_id: object,
    db: Session | None = None,
    *,
    require_durable: bool = False,
) -> None:
    """Invalidate every existing customer portal session for a subscriber."""
    ttl = max(
        _session_ttl_seconds(remember=True, db=db),
        _session_ttl_seconds(remember=False, db=db),
        _absolute_ttl_seconds(db),
    )
    set_session_revocation_epoch(
        _CUSTOMER_SESSION_PREFIX,
        str(subscriber_id),
        ttl,
        _CUSTOMER_SESSION_EPOCHS,
        require_durable=require_durable,
    )


def revoke_other_customer_sessions_for_subscriber(
    subscriber_id: object,
    current_session_token: str | None,
    db: Session | None = None,
) -> None:
    """Invalidate a subscriber's other portal sessions while keeping this one."""
    current_session = (
        load_session(
            _CUSTOMER_SESSION_PREFIX, current_session_token, _CUSTOMER_SESSIONS
        )
        if current_session_token
        else None
    )
    ttl = max(
        _session_ttl_seconds(remember=True, db=db),
        _session_ttl_seconds(remember=False, db=db),
        _absolute_ttl_seconds(db),
    )
    epoch = set_session_revocation_epoch(
        _CUSTOMER_SESSION_PREFIX, str(subscriber_id), ttl, _CUSTOMER_SESSION_EPOCHS
    )
    if not current_session or str(current_session.get("subscriber_id")) != str(
        subscriber_id
    ):
        return
    now = datetime.now(UTC)
    try:
        expires_at = datetime.fromisoformat(str(current_session["expires_at"]))
    except (KeyError, ValueError, TypeError):
        return
    if now >= expires_at:
        invalidate_customer_session(current_session_token or "")
        return
    epoch_at = datetime.fromisoformat(epoch)
    current_session["revocation_exempted_at"] = (
        epoch_at + timedelta(microseconds=1)
    ).isoformat()
    store_session(
        _CUSTOMER_SESSION_PREFIX,
        current_session_token or "",
        current_session,
        max(1, int((expires_at - now).total_seconds())),
        _CUSTOMER_SESSIONS,
        principal_id=str(subscriber_id),
        fallback_index=_CUSTOMER_SESSION_INDEX,
    )


def refresh_customer_session(
    session_token: str, db: Session | None = None
) -> dict | None:
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    if not session:
        return None

    now = datetime.now(UTC)
    expires_at = _cache_timestamp(session.get("expires_at"))
    if expires_at is None or now >= expires_at:
        invalidate_customer_session(session_token)
        return None

    identity = _standing_query_from_cache(session)
    created = _cache_timestamp(session.get("created_at"))
    if (
        _customer_session_revoked(session)
        or db is None
        or identity is None
        or created is None
    ):
        invalidate_customer_session(session_token)
        return None
    outcome = resolve_customer_session_refresh(
        db,
        query=CustomerSessionRefreshQuery(
            standing=identity,
            created_at=created,
            remember=bool(session.get("remember")),
            now=now,
        ),
    )
    if outcome is None:
        invalidate_customer_session(session_token)
        return None
    new_expires_at = outcome.expires_at
    session["expires_at"] = new_expires_at.isoformat()
    store_session(
        _CUSTOMER_SESSION_PREFIX,
        session_token,
        session,
        outcome.ttl_seconds,
        _CUSTOMER_SESSIONS,
        principal_id=str(session.get("subscriber_id"))
        if session.get("subscriber_id")
        else None,
        fallback_index=_CUSTOMER_SESSION_INDEX,
    )
    return session


def invalidate_customer_session(session_token: str) -> None:
    """Invalidate a customer session."""
    session = load_session(_CUSTOMER_SESSION_PREFIX, session_token, _CUSTOMER_SESSIONS)
    principal_id = str(session.get("subscriber_id")) if session else None
    delete_session(
        _CUSTOMER_SESSION_PREFIX,
        session_token,
        _CUSTOMER_SESSIONS,
        principal_id=principal_id,
        fallback_index=_CUSTOMER_SESSION_INDEX,
    )


def list_customer_sessions_for_subscriber(
    subscriber_id: object,
    current_session_token: str | None = None,
) -> list[dict[str, object]]:
    """List currently valid customer portal sessions for a subscriber."""
    sessions = []
    for token, payload in list_sessions_for_principal(
        _CUSTOMER_SESSION_PREFIX,
        str(subscriber_id),
        _CUSTOMER_SESSIONS,
        _CUSTOMER_SESSION_INDEX,
    ):
        active_payload = get_customer_session(token)
        if not active_payload:
            continue
        sessions.append(
            {
                "token": token,
                "created_at": active_payload.get("created_at"),
                "expires_at": active_payload.get("expires_at"),
                "is_current": bool(
                    current_session_token and token == current_session_token
                ),
                "remember": bool(active_payload.get("remember")),
                "username": active_payload.get("username"),
            }
        )
    sessions.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return sessions


def resolve_customer_session_standing(
    db: Session,
    *,
    query: CustomerSessionStandingQuery,
) -> CustomerSessionStanding | None:
    """Database-authoritative read. ORM state never crosses the query boundary."""
    row = db.get(AuthSession, query.auth_session_id, populate_existing=True)
    if (
        row is None
        or row.subscriber_id != query.subscriber_id
        or query.account_id != query.subscriber_id
        or row.status != SessionStatus.active
        or row.revoked_at is not None
        or row.expires_at is None
    ):
        return None
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if expiry <= datetime.now(UTC):
        return None
    return CustomerSessionStanding(query.auth_session_id, query.subscriber_id, expiry)


def _standing_query_from_cache(session: dict) -> CustomerSessionStandingQuery | None:
    # Cache mappings are serialized transport data, converted here.
    try:
        return CustomerSessionStandingQuery(
            auth_session_id=UUID(str(session.get("auth_session_id"))),
            subscriber_id=UUID(str(session.get("subscriber_id"))),
            account_id=UUID(str(session.get("account_id"))),
        )
    except (ValueError, TypeError, AttributeError):
        return None


def _has_auth_session_standing(db: Session, session: dict) -> bool:
    query = _standing_query_from_cache(session)
    return (
        query is not None
        and resolve_customer_session_standing(db, query=query) is not None
    )


def resolve_customer_session_refresh(
    db: Session,
    *,
    query: CustomerSessionRefreshQuery,
) -> CustomerSessionRefreshOutcome | None:
    standing = resolve_customer_session_standing(db, query=query.standing)
    if standing is None:
        return None
    expiry = min(
        query.now + timedelta(seconds=_session_ttl_seconds(query.remember, db)),
        query.created_at + timedelta(seconds=_absolute_ttl_seconds(db)),
        standing.expires_at,
    )
    if expiry <= query.now:
        return None
    return CustomerSessionRefreshOutcome(
        expiry, max(1, int((expiry - query.now).total_seconds()))
    )


def get_current_customer(session_token: str | None, db: Session) -> dict | None:
    """Resolve a customer session token into a hydrated session dict."""
    if not session_token:
        return None

    session = get_customer_session(session_token)
    if not session:
        return None

    # Redis is a delivery projection, never authentication authority. Legacy
    # ordinary and impersonation payloads without standing must sign in again.
    if not _has_auth_session_standing(db, session):
        invalidate_customer_session(session_token)
        return None

    # Identity is bound at issuance. Never re-resolve it from a potentially
    # colliding local/RADIUS/access username on subsequent requests.
    subscription_id = session.get("subscription_id")
    if subscription_id:
        try:
            subscription = db.get(Subscription, UUID(str(subscription_id)))
        except (ValueError, TypeError, AttributeError):
            subscription = None
        if subscription is None or str(subscription.subscriber_id) != str(
            session["subscriber_id"]
        ):
            invalidate_customer_session(session_token)
            return None

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
