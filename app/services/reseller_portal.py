import logging
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException, Request, status
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

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
from app.models.catalog import CatalogOffer, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.enforcement_lock import EnforcementReason
from app.models.subscriber import (
    Reseller,
    ResellerUser,
    Subscriber,
    SubscriberStatus,
    UserType,
)
from app.services import catalog as catalog_service
from app.services import customer_portal
from app.services.account_lifecycle import (
    compute_account_status,
    get_active_locks,
    resolve_all_locks,
    restore_subscription,
    suspend_subscription,
)
from app.services.common import coerce_uuid
from app.services.session_store import (
    delete_session,
    get_session_revocation_epoch,
    list_sessions_for_principal,
    load_session,
    set_session_revocation_epoch,
    store_session,
)
from app.services.settings_spec import resolve_value
from app.services.topology.connection_status import connection_status

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
_DEFAULT_ABSOLUTE_TTL = 2592000  # 30 days

_RESELLER_SESSIONS: dict[str, dict] = {}
_RESELLER_SESSION_INDEX: dict[str, set[str]] = {}
_RESELLER_SESSION_EPOCHS: dict[str, str] = {}
_RESELLER_SESSION_PREFIX = "session:reseller_portal"


def _now() -> datetime:
    return datetime.now(UTC)


def _initials(subscriber: Subscriber) -> str:
    first = (subscriber.first_name or "").strip()[:1]
    last = (subscriber.last_name or "").strip()[:1]
    initials = f"{first}{last}".upper()
    return initials or "RS"


def _initials_from_name(name: str) -> str:
    """Initials for a reseller_user principal (no first/last split)."""
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "RS"
    if len(parts) == 1:
        return parts[0][:2].upper() or "RS"
    return f"{parts[0][:1]}{parts[-1][:1]}".upper()


def _subscriber_label(subscriber: Subscriber | None) -> str:
    if not subscriber:
        return "Account"
    # Backwards-compat: older code treats SubscriberAccount as having a `.person`
    # relationship.
    person = getattr(subscriber, "person", None)
    base = person or subscriber

    def _clean_str(value: object | None) -> str:
        if isinstance(value, str):
            return value.strip()
        return ""

    legal_name = _clean_str(getattr(base, "legal_name", None))
    company_name = _clean_str(getattr(base, "company_name", None))
    if legal_name:
        return legal_name
    if company_name:
        return company_name
    first = _clean_str(getattr(base, "first_name", None))
    last = _clean_str(getattr(base, "last_name", None))
    display = f"{first} {last}".strip()
    display_name = _clean_str(getattr(base, "display_name", None))
    return display or display_name or "Customer"


def _customer_accounts_query(db: Session, reseller_id: str):
    reseller_uuid = coerce_uuid(reseller_id)
    # Lazy import avoids any import cycle with the subscriber service.
    from app.services.subscriber import not_soft_deleted_subscriber_clause

    return (
        db.query(Subscriber)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(
            or_(
                Subscriber.user_type.is_(None),
                Subscriber.user_type != UserType.reseller,
            )
        )
        # Exclude soft-deleted (canceled) accounts so they don't leak into the
        # accounts list, counts, drill-down, and the alert/revenue metrics that
        # build on this query.
        .filter(not_soft_deleted_subscriber_clause())
    )


def _get_customer_account(
    db: Session,
    reseller_id: str,
    account_id: str,
) -> Subscriber | None:
    return (
        _customer_accounts_query(db, reseller_id)
        .filter(Subscriber.id == coerce_uuid(account_id))
        .first()
    )


def owned_account(db: Session, reseller_id: str, account_id: str) -> Subscriber | None:
    """Return the customer account (Subscriber) iff it belongs to this reseller,
    else None. Public ownership check for reseller-scoped endpoints."""
    return _get_customer_account(db, reseller_id, account_id)


def _customer_account_join_filter():
    # Lazy import avoids any import cycle with the subscriber service.
    from app.services.subscriber import not_soft_deleted_subscriber_clause

    # Applied on Invoice/Payment ⨝ Subscriber joins so the money aggregations
    # (open balance, overdue, revenue) exclude soft-deleted accounts too.
    return and_(
        or_(
            Subscriber.user_type.is_(None),
            Subscriber.user_type != UserType.reseller,
        ),
        not_soft_deleted_subscriber_clause(),
    )


def portal_user_subscriber_ids(db: Session, reseller_id: str) -> list[str]:
    """Subscriber ids of the reseller's active portal users (push targets).

    Each reseller-portal login is backed by a subscriber_id under which the
    mobile app registers device tokens; subscriber-less (Layer-3) logins are
    excluded. Returns [] on schemas without the reseller_users table."""
    try:
        rows = (
            db.query(ResellerUser.subscriber_id)
            .filter(ResellerUser.reseller_id == coerce_uuid(reseller_id))
            .filter(ResellerUser.is_active.is_(True))
            .filter(ResellerUser.subscriber_id.isnot(None))
            .all()
        )
        return [str(sid) for (sid,) in rows]
    except ProgrammingError:
        db.rollback()
        return []


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


def reseller_id_for_subscriber(db: Session, subscriber_id: str) -> str | None:
    """Return the reseller_id the subscriber administers, or None when they are
    not an active reseller user. Used by the bearer reseller API to scope every
    request to the caller's own reseller."""
    reseller_user = _get_reseller_user(db, subscriber_id)
    if reseller_user is None or not getattr(reseller_user, "reseller_id", None):
        return None
    return str(reseller_user.reseller_id)


def create_reseller_user_principal(
    db: Session,
    *,
    reseller_id: str,
    username: str,
    password: str,
    email: str | None = None,
    full_name: str | None = None,
    must_change_password: bool = False,
) -> ResellerUser:
    """Create a first-class reseller portal login (Layer 3).

    A ``ResellerUser`` identity plus its local ``UserCredential`` — no backing
    Subscriber. Used by reseller onboarding (post-cutover) and by the backfill
    that repoints existing reseller credentials. The login only authenticates
    when ``RESELLER_USER_PRINCIPAL_ENABLED`` is on.
    """
    from datetime import UTC, datetime

    from app.models.auth import AuthProvider, UserCredential

    reseller_user = ResellerUser(
        reseller_id=coerce_uuid(reseller_id),
        email=email,
        full_name=full_name,
        is_active=True,
    )
    db.add(reseller_user)
    db.flush()
    credential = UserCredential(
        reseller_user_id=reseller_user.id,
        provider=AuthProvider.local,
        username=username,
        password_hash=auth_flow_service.hash_password(password),
        must_change_password=must_change_password,
        password_updated_at=datetime.now(UTC),
        is_active=True,
    )
    db.add(credential)
    db.commit()
    db.refresh(reseller_user)
    return reseller_user


def _create_session(
    username: str,
    reseller_id: str,
    remember: bool,
    subscriber_id: str | None = None,
    db: Session | None = None,
    person_id: str | None = None,
    auth_session_id: str | None = None,
    reseller_user_id: str | None = None,
    is_impersonation: bool = False,
    return_to: str | None = None,
) -> str:
    if not subscriber_id:
        subscriber_id = person_id
    if not subscriber_id and not reseller_user_id:
        raise ValueError("subscriber_id/person_id or reseller_user_id is required")
    # Layer 3: a reseller login may be a first-class ResellerUser principal (no
    # backing subscriber). principal_type/principal_id generalise the session
    # key; subscriber_id/person_id stay populated for the legacy path.
    principal_type = "reseller_user" if reseller_user_id else "subscriber"
    principal_id = reseller_user_id or subscriber_id
    session_token = secrets.token_urlsafe(32)
    ttl_seconds = _session_ttl_seconds(remember, db)
    session_payload = {
        "username": username,
        "subscriber_id": subscriber_id,
        # Backwards-compat: older tests/callers use "person_id".
        "person_id": subscriber_id,
        "reseller_user_id": reseller_user_id,
        "principal_type": principal_type,
        "principal_id": principal_id,
        "reseller_id": reseller_id,
        # Backing auth_flow session, so logout can revoke it (not just drop
        # the local portal session).
        "auth_session_id": auth_session_id,
        "remember": remember,
        # Admin "view as reseller": an admin impersonation session keeps the
        # reseller principal but is flagged so the portal shows an exit banner
        # and the stop endpoint can return the admin to ``return_to``.
        "is_impersonation": is_impersonation,
        "return_to": return_to,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    store_session(
        _RESELLER_SESSION_PREFIX,
        session_token,
        session_payload,
        ttl_seconds,
        _RESELLER_SESSIONS,
        principal_id=str(principal_id) if principal_id else None,
        fallback_index=_RESELLER_SESSION_INDEX,
    )
    return session_token


def resolve_impersonation_principal(
    db: Session, reseller_id: str
) -> ResellerUser | None:
    """Pick the reseller login an admin should "view as".

    Mirrors customer impersonation, which targets a real subscriber: here we
    target a real reseller principal so ``get_context`` works unchanged. Prefer
    an active ``ResellerUser`` for the reseller (Layer 3 standalone or
    subscriber-backed); fall back to a legacy subscriber whose ``user_type`` is
    ``reseller``. Returns ``None`` when the reseller has no portal login.
    """
    reseller_uuid = coerce_uuid(reseller_id)
    try:
        reseller_user = (
            db.query(ResellerUser)
            .filter(ResellerUser.reseller_id == reseller_uuid)
            .filter(ResellerUser.is_active.is_(True))
            .order_by(ResellerUser.created_at.asc())
            .first()
        )
        if reseller_user:
            return reseller_user
    except ProgrammingError:
        # Schema without the reseller_users table — fall through to legacy.
        db.rollback()

    subscriber = (
        db.query(Subscriber)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(Subscriber.is_active.is_(True))
        .filter(Subscriber.user_type == UserType.reseller)
        .order_by(Subscriber.created_at.asc())
        .first()
    )
    if not subscriber:
        return None
    return SimpleNamespace(
        id=subscriber.id,
        subscriber_id=subscriber.id,
        person_id=subscriber.id,
        reseller_id=subscriber.reseller_id,
        is_active=True,
        created_at=subscriber.created_at,
    )


def create_impersonation_session(
    db: Session,
    *,
    reseller_id: str,
    return_to: str,
) -> str:
    """Mint a reseller portal session for an admin to "view as" the reseller.

    Raises ``HTTPException(404)`` when the reseller has no login principal to
    impersonate (e.g. an org that has never had a portal user provisioned).
    """
    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if not reseller:
        raise HTTPException(status_code=404, detail="Reseller not found")

    principal = resolve_impersonation_principal(db, reseller_id)
    if principal is None:
        raise HTTPException(
            status_code=404,
            detail="This reseller has no portal login to view as.",
        )

    # A subscriber-backed principal carries subscriber_id; a Layer-3 standalone
    # ResellerUser has subscriber_id == None and is keyed by reseller_user_id.
    subscriber_id = getattr(principal, "subscriber_id", None)
    reseller_user_id = None if subscriber_id else principal.id
    return _create_session(
        username=f"impersonate:reseller:{reseller_id}",
        reseller_id=str(reseller.id),
        remember=False,
        subscriber_id=str(subscriber_id) if subscriber_id else None,
        reseller_user_id=str(reseller_user_id) if reseller_user_id else None,
        db=db,
        is_impersonation=True,
        return_to=return_to,
    )


def _get_session(session_token: str) -> dict | None:
    session = load_session(_RESELLER_SESSION_PREFIX, session_token, _RESELLER_SESSIONS)
    if not session:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    if _now() > expires_at:
        invalidate_session(session_token)
        return None
    if _reseller_session_revoked(session):
        invalidate_session(session_token)
        return None
    return session


def _reseller_session_revoked(session: dict) -> bool:
    """True when a revoke-all epoch postdates this session's creation."""
    principal_id = (
        session.get("principal_id")
        or session.get("subscriber_id")
        or session.get("person_id")
    )
    if not principal_id:
        return False
    epoch = get_session_revocation_epoch(
        _RESELLER_SESSION_PREFIX, str(principal_id), _RESELLER_SESSION_EPOCHS
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


def revoke_reseller_sessions_for_subscriber(
    subscriber_id: object, db: Session | None = None
) -> None:
    """Invalidate every existing reseller portal session for a subscriber."""
    revoke_reseller_sessions_for_principal(subscriber_id, db=db)


def revoke_reseller_sessions_for_principal(
    principal_id: object, db: Session | None = None
) -> None:
    """Invalidate every existing reseller portal session for a principal."""
    ttl = max(
        _session_ttl_seconds(remember=True, db=db),
        _session_ttl_seconds(remember=False, db=db),
        _absolute_ttl_seconds(db),
    )
    set_session_revocation_epoch(
        _RESELLER_SESSION_PREFIX, str(principal_id), ttl, _RESELLER_SESSION_EPOCHS
    )


def revoke_other_reseller_sessions_for_principal(
    principal_id: object,
    current_session_token: str | None,
    db: Session | None = None,
) -> None:
    """Invalidate a reseller principal's other sessions while keeping this one."""
    current_session = (
        load_session(
            _RESELLER_SESSION_PREFIX, current_session_token, _RESELLER_SESSIONS
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
        _RESELLER_SESSION_PREFIX, str(principal_id), ttl, _RESELLER_SESSION_EPOCHS
    )
    if not current_session or str(
        current_session.get("principal_id")
        or current_session.get("subscriber_id")
        or current_session.get("person_id")
    ) != str(principal_id):
        return
    now = _now()
    try:
        expires_at = datetime.fromisoformat(str(current_session["expires_at"]))
    except (KeyError, ValueError, TypeError):
        return
    if now >= expires_at:
        invalidate_session(current_session_token or "", db=db)
        return
    epoch_at = datetime.fromisoformat(epoch)
    current_session["revocation_exempted_at"] = (
        epoch_at + timedelta(microseconds=1)
    ).isoformat()
    store_session(
        _RESELLER_SESSION_PREFIX,
        current_session_token or "",
        current_session,
        max(1, int((expires_at - now).total_seconds())),
        _RESELLER_SESSIONS,
        principal_id=str(principal_id),
        fallback_index=_RESELLER_SESSION_INDEX,
    )


def _revoke_auth_session(db: Session, auth_session_id: str | None) -> None:
    """Revoke the backing auth_flow session so logout actually ends access."""
    if not auth_session_id:
        return
    try:
        auth_session = db.get(AuthSession, coerce_uuid(auth_session_id))
        if auth_session and auth_session.status == SessionStatus.active:
            auth_session.status = SessionStatus.revoked
            db.commit()
    except Exception:
        db.rollback()
        logger.warning("Failed to revoke reseller auth session", exc_info=True)


def invalidate_session(session_token: str, db: Session | None = None) -> None:
    # Read raw session without going through _get_session (which calls invalidate on expiry)
    session = load_session(_RESELLER_SESSION_PREFIX, session_token, _RESELLER_SESSIONS)
    principal_id = (
        str(
            session.get("principal_id")
            or session.get("subscriber_id")
            or session.get("person_id")
        )
        if session
        else None
    )
    delete_session(
        _RESELLER_SESSION_PREFIX,
        session_token,
        _RESELLER_SESSIONS,
        principal_id=principal_id,
        fallback_index=_RESELLER_SESSION_INDEX,
    )
    if db and session:
        _revoke_auth_session(db, session.get("auth_session_id"))
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
    principal_id = payload.get("sub")
    principal_type = payload.get("principal_type") or "subscriber"
    session_id = payload.get("session_id")
    if not principal_id or not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        )

    auth_session = db.get(AuthSession, coerce_uuid(session_id))
    if not auth_session or auth_session.status != SessionStatus.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session"
        )
    session_expires = auth_session.expires_at
    if session_expires:
        # Normalise both sides: SQLite returns naive datetimes and some callers
        # patch _now() to a naive value, so compare consistently in UTC.
        now = _now()
        if session_expires.tzinfo is None:
            session_expires = session_expires.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if session_expires <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
            )

    # Layer 3: a reseller_user principal is its own identity — resolve the
    # reseller directly, with no backing subscriber.
    if principal_type == "reseller_user":
        reseller_user = db.get(ResellerUser, coerce_uuid(principal_id))
        if (
            not reseller_user
            or not reseller_user.is_active
            or not reseller_user.reseller_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Reseller access required"
            )
        display = reseller_user.full_name or reseller_user.email or "Reseller"
        session_token = _create_session(
            username=username or reseller_user.email or display,
            reseller_user_id=str(reseller_user.id),
            reseller_id=str(reseller_user.reseller_id),
            remember=remember,
            db=db,
            auth_session_id=str(session_id),
        )
        _emit_reseller_event(
            db,
            "reseller_login",
            {
                "reseller_id": str(reseller_user.reseller_id),
                "reseller_user_id": str(reseller_user.id),
            },
        )
        return {
            "session_token": session_token,
            "reseller_id": str(reseller_user.reseller_id),
        }

    reseller_user = _get_reseller_user(db, str(principal_id))
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
        auth_session_id=str(session_id),
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
        "session_token": str(session_token),
        "reseller_id": str(reseller_user.reseller_id),
    }


def get_context(db: Session, session_token: str | None) -> dict | None:
    session = _get_session(session_token or "")
    if not session:
        return None

    reseller = db.get(Reseller, coerce_uuid(session["reseller_id"]))
    if not reseller:
        return None

    # Layer 3: reseller_user principal — identity comes from the ResellerUser
    # row, with no backing subscriber.
    if (session.get("principal_type") or "subscriber") == "reseller_user":
        ru_id = session.get("reseller_user_id") or session.get("principal_id")
        reseller_user = db.get(ResellerUser, coerce_uuid(ru_id)) if ru_id else None
        if not reseller_user or not reseller_user.is_active:
            return None
        display = reseller_user.full_name or reseller_user.email or "Reseller"
        current_user = {
            "name": display,
            "email": reseller_user.email or "",
            "initials": _initials_from_name(display),
        }
        return {
            "session": session,
            "current_user": current_user,
            "person": None,
            "subscriber": None,
            "reseller": reseller,
            "reseller_user": reseller_user,
            # Principal-agnostic acting identity (Layer 3): handlers should key
            # MFA/audit/personal-feature scoping off these, not context["subscriber"]
            # which is None for a first-class reseller_user principal.
            "principal_type": "reseller_user",
            "principal_id": str(reseller_user.id),
            "is_impersonation": bool(session.get("is_impersonation")),
            "return_to": session.get("return_to"),
        }

    subscriber = db.get(Subscriber, coerce_uuid(session["subscriber_id"]))
    if not subscriber:
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
        "principal_type": "subscriber",
        "principal_id": str(subscriber.id),
        "is_impersonation": bool(session.get("is_impersonation")),
        "return_to": session.get("return_to"),
    }


def refresh_session(
    session_token: str | None, db: Session | None = None
) -> dict | None:
    if not session_token:
        return None
    session = _get_session(session_token)
    if not session:
        return None
    now = _now()
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
                invalidate_session(session_token)
                return None
            new_expires_at = min(new_expires_at, absolute_limit)
    session["expires_at"] = new_expires_at.isoformat()
    store_session(
        _RESELLER_SESSION_PREFIX,
        session_token,
        session,
        max(1, int((new_expires_at - now).total_seconds())),
        _RESELLER_SESSIONS,
        principal_id=str(
            session.get("principal_id") or session.get("subscriber_id") or ""
        )
        or None,
        fallback_index=_RESELLER_SESSION_INDEX,
    )
    return session


def list_reseller_sessions_for_principal(
    principal_id: object,
    current_session_token: str | None = None,
) -> list[dict[str, object]]:
    """List currently valid reseller portal sessions for a principal."""
    sessions = []
    for token, payload in list_sessions_for_principal(
        _RESELLER_SESSION_PREFIX,
        str(principal_id),
        _RESELLER_SESSIONS,
        _RESELLER_SESSION_INDEX,
    ):
        active_payload = _get_session(token)
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
                "principal_type": active_payload.get("principal_type") or "subscriber",
            }
        )
    sessions.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return sessions


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


def _absolute_ttl_seconds(db: Session | None = None) -> int:
    """Absolute cap on a session's lifetime regardless of sliding refreshes."""
    ttl = (
        resolve_value(db, SettingDomain.auth, "reseller_session_absolute_ttl_seconds")
        if db
        else None
    )
    if ttl is None:
        return _DEFAULT_ABSOLUTE_TTL
    try:
        return int(str(ttl))
    except (TypeError, ValueError):
        return _DEFAULT_ABSOLUTE_TTL


def get_session_max_age(db: Session | None = None) -> int:
    """Get the session max age for non-remember sessions."""
    return _session_ttl_seconds(remember=False, db=db)


def get_remember_max_age(db: Session | None = None) -> int:
    """Get the session max age for remember-me sessions."""
    return _session_ttl_seconds(remember=True, db=db)


ACCOUNT_ORDER_FIELDS = ("created_at", "balance", "overdue", "name")
ACCOUNT_STATUS_FILTERS = ("overdue",) + tuple(
    status.value for status in SubscriberStatus
)
ACCOUNT_LIST_STATUS_OPTIONS = tuple(status.value for status in SubscriberStatus)
RESELLER_ACCOUNT_STATUS_ACTIONS = ("deactivate", "restore", "disable")


def _apply_account_search(query, search: str | None):
    if not search:
        return query
    pattern = f"%{search.strip()}%"
    return query.filter(
        (Subscriber.first_name.ilike(pattern))
        | (Subscriber.last_name.ilike(pattern))
        | (Subscriber.email.ilike(pattern))
        | (Subscriber.account_number.ilike(pattern))
        | (Subscriber.phone.ilike(pattern))
    )


def _open_invoice_subquery(db: Session):
    """Per-account open balance / open + overdue invoice counts."""
    from sqlalchemy import case

    open_statuses = (
        InvoiceStatus.issued,
        InvoiceStatus.partially_paid,
        InvoiceStatus.overdue,
    )
    return (
        db.query(
            Invoice.account_id.label("aid"),
            func.coalesce(func.sum(Invoice.balance_due), 0).label("open_balance"),
            func.count(Invoice.id).label("open_invoices"),
            func.sum(case((Invoice.status == InvoiceStatus.overdue, 1), else_=0)).label(
                "overdue_invoices"
            ),
        )
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(open_statuses))
        .group_by(Invoice.account_id)
        .subquery()
    )


def _filtered_accounts_query(
    db: Session,
    reseller_id: str,
    search: str | None,
    status_filter: str | None,
    invoice_sq,
):
    query = _customer_accounts_query(db, reseller_id).outerjoin(
        invoice_sq, invoice_sq.c.aid == Subscriber.id
    )
    query = _apply_account_search(query, search)
    if status_filter == "overdue":
        query = query.filter(func.coalesce(invoice_sq.c.overdue_invoices, 0) > 0)
    elif status_filter in ACCOUNT_LIST_STATUS_OPTIONS:
        query = query.filter(Subscriber.status == SubscriberStatus(status_filter))
    else:
        query = query.filter(
            or_(
                Subscriber.status.is_(None),
                Subscriber.status != SubscriberStatus.disabled,
            )
        )
    return query


def list_accounts(
    db: Session,
    reseller_id: str,
    limit: int,
    offset: int,
    search: str | None = None,
    status_filter: str | None = None,
    order_by: str = "created_at",
    order_dir: str = "desc",
) -> list[dict]:
    invoice_sq = _open_invoice_subquery(db)
    query = _filtered_accounts_query(db, reseller_id, search, status_filter, invoice_sq)

    descending = order_dir != "asc"
    if order_by == "balance":
        sort_col = func.coalesce(invoice_sq.c.open_balance, 0)
    elif order_by == "overdue":
        sort_col = func.coalesce(invoice_sq.c.overdue_invoices, 0)
    elif order_by == "name":
        sort_col = func.lower(
            func.coalesce(Subscriber.display_name, Subscriber.first_name)
        )
        descending = order_dir == "desc"
    else:
        sort_col = Subscriber.created_at
    query = query.order_by(
        sort_col.desc() if descending else sort_col.asc(),
        Subscriber.id,
    )

    accounts = query.limit(limit).offset(offset).all()
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
        .filter(Invoice.is_active.is_(True))
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


def _subscription_label(subscription: Subscription | None) -> str:
    if subscription is None:
        return "No service"
    offer = getattr(subscription, "offer", None)
    if offer is not None and getattr(offer, "name", None):
        return str(offer.name)
    return "Internet service"


def _subscriptions_for_connection_status(
    db: Session, account_id
) -> list[Subscription | None]:
    """Return every active connection, falling back to latest inactive service."""
    active = (
        db.query(Subscription)
        .outerjoin(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .filter(Subscription.subscriber_id == account_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.created_at.desc())
        .all()
    )
    if active:
        return active

    latest = (
        db.query(Subscription)
        .outerjoin(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .filter(Subscription.subscriber_id == account_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    return [latest]


def _inactive_connection_status(subscription: Subscription | None) -> dict:
    if subscription is None:
        return {
            "state": "trouble",
            "headline": "No active service",
            "message": "No active service is available for this customer.",
            "advice": None,
            "medium": None,
            "area_outage": False,
            "checked_at": None,
        }
    status_value = subscription.status.value if subscription.status else "unknown"
    return {
        "state": "trouble",
        "headline": f"Service {status_value.replace('_', ' ')}",
        "message": "The customer's service is not currently active.",
        "advice": None,
        "medium": None,
        "area_outage": False,
        "checked_at": None,
    }


def list_customer_connection_statuses(
    db: Session,
    reseller_id: str,
    limit: int,
    offset: int = 0,
) -> dict:
    """Customer-safe connection statuses for accounts owned by one reseller."""
    accounts = (
        _customer_accounts_query(db, reseller_id)
        .order_by(
            func.lower(func.coalesce(Subscriber.display_name, Subscriber.first_name)),
            Subscriber.id,
        )
        .limit(limit)
        .offset(offset)
        .all()
    )
    rows = []
    counts = {"connected": 0, "trouble": 0, "outage": 0, "unknown": 0}
    for account in accounts:
        for subscription in _subscriptions_for_connection_status(db, account.id):
            if (
                subscription is not None
                and subscription.status == SubscriptionStatus.active
            ):
                try:
                    status_payload = connection_status(db, subscription)
                except Exception:
                    logger.warning(
                        "Could not resolve reseller customer connection status",
                        extra={
                            "account_id": str(account.id),
                            "subscription_id": str(subscription.id),
                        },
                        exc_info=True,
                    )
                    status_payload = {
                        "state": "unknown",
                        "headline": "Status unavailable",
                        "message": "We couldn't check this customer's connection.",
                        "advice": None,
                        "medium": None,
                        "area_outage": False,
                        "checked_at": None,
                    }
            else:
                status_payload = _inactive_connection_status(subscription)

            state = str(status_payload.get("state") or "unknown")
            counts[state if state in counts else "unknown"] += 1
            rows.append(
                {
                    "account_id": str(account.id),
                    "account_number": account.account_number,
                    "subscriber_name": _subscriber_label(account),
                    "account_status": (
                        account.status.value if account.status else "active"
                    ),
                    "subscription_id": str(subscription.id) if subscription else None,
                    "subscription_name": _subscription_label(subscription),
                    "subscription_status": (
                        subscription.status.value
                        if subscription is not None and subscription.status
                        else None
                    ),
                    "state": state,
                    "headline": status_payload.get("headline"),
                    "message": status_payload.get("message"),
                    "medium": status_payload.get("medium"),
                    "area_outage": bool(status_payload.get("area_outage")),
                    "checked_at": status_payload.get("checked_at"),
                }
            )

    return {
        "rows": rows,
        "counts": counts,
        "total": (
            _customer_accounts_query(db, reseller_id)
            .with_entities(func.count(Subscriber.id))
            .scalar()
            or 0
        ),
    }


def count_accounts(
    db: Session,
    reseller_id: str,
    search: str | None = None,
    status_filter: str | None = None,
) -> int:
    invoice_sq = _open_invoice_subquery(db)
    query = _filtered_accounts_query(db, reseller_id, search, status_filter, invoice_sq)
    return query.with_entities(func.count(Subscriber.id)).scalar() or 0


def get_dashboard_summary(
    db: Session,
    reseller_id: str,
    limit: int,
    offset: int,
) -> dict:
    accounts = list_accounts(db, reseller_id, limit, offset)

    total_accounts = (
        _customer_accounts_query(db, reseller_id)
        .with_entities(func.count(Subscriber.id))
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
        .filter(_customer_account_join_filter())
        .filter(Invoice.is_active.is_(True))
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
        .filter(_customer_account_join_filter())
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status == InvoiceStatus.overdue)
        .scalar()
        or 0
    )

    week_ago = datetime.now(UTC) - timedelta(days=7)
    new_this_week = (
        _customer_accounts_query(db, reseller_id)
        .with_entities(func.count(Subscriber.id))
        .filter(Subscriber.created_at >= week_ago)
        .scalar()
        or 0
    )

    from app.models.subscriber import SubscriberStatus

    suspended_count = (
        _customer_accounts_query(db, reseller_id)
        .with_entities(func.count(Subscriber.id))
        .filter(Subscriber.status == SubscriberStatus.suspended)
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
                "action_url": "/reseller/billing",
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
    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
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
        .filter(
            Invoice.account_id == account.id,
            Invoice.is_active.is_(True),
            Invoice.status.in_(open_statuses),
        )
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


def update_customer_account_status(
    db: Session,
    reseller_id: str,
    account_id: str,
    action: str,
    *,
    actor_id: str | None = None,
) -> dict | None:
    """Deactivate, restore, or disable a reseller-owned customer account."""
    normalized_action = (action or "").strip().lower()
    if normalized_action not in RESELLER_ACCOUNT_STATUS_ACTIONS:
        raise ValueError("Unsupported account status action")

    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
        return None

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == account.id)
        .with_for_update()
        .all()
    )
    source = f"reseller:{reseller_id}"
    if actor_id:
        source = f"{source}:user:{actor_id}"

    changed = 0
    skipped = 0
    if normalized_action == "deactivate":
        for subscription in subscriptions:
            if subscription.status in {
                SubscriptionStatus.active,
                SubscriptionStatus.pending,
                SubscriptionStatus.blocked,
                SubscriptionStatus.stopped,
            }:
                if get_active_locks(db, subscription_id=str(subscription.id)):
                    skipped += 1
                    continue
                # Route through the domain op so deactivation creates an
                # enforcement lock (single writer). A bare ``status = stopped``
                # produced a lock-less state that the domain ``restore`` path
                # could not reactivate (resolved_count == 0) — the split-brain
                # this fixes. active/pending → suspended; an already-down
                # blocked/stopped keeps its status but gains the missing lock.
                # emit=True (the default): the subscription_suspended event is what
                # actually ENFORCES the suspension — it blocks the credential in
                # RADIUS and disconnects the live session. With emit=False this
                # path created the lock and flipped the status while the customer
                # kept browsing until the next populate() sweep. The lock is not
                # the enforcement; the event is.
                suspend_subscription(
                    db,
                    str(subscription.id),
                    reason=EnforcementReason.admin,
                    source=source,
                    notes="Deactivated from reseller portal.",
                )
                changed += 1
            else:
                skipped += 1
        db.flush()
        compute_account_status(db, str(account.id))
    elif normalized_action == "restore":
        restorable_statuses = {
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        }
        for subscription in subscriptions:
            if subscription.status not in restorable_statuses:
                skipped += 1
                continue
            restored = restore_subscription(
                db,
                str(subscription.id),
                trigger="admin",
                resolved_by=source,
                reason=EnforcementReason.admin,
                notes="Restored from reseller portal.",
            )
            # No local fallback. restore_subscription now restores a lock-less
            # suspended subscription itself, so a raw ``status = active`` here —
            # which emitted no event, left the IP unprovisioned and RADIUS
            # unsynced — is no longer needed. If the owner declines, it declined
            # for a reason (an unauthorized trigger, or an active login on the
            # login name), and the caller must not override it.
            if restored:
                changed += 1
            else:
                skipped += 1
        db.flush()
        compute_account_status(db, str(account.id))
    else:
        terminal_statuses = {
            SubscriptionStatus.disabled,
            SubscriptionStatus.canceled,
            SubscriptionStatus.expired,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        }
        for subscription in subscriptions:
            if subscription.status in terminal_statuses:
                skipped += 1
                continue
            resolve_all_locks(db, subscription, source)
            subscription.status = SubscriptionStatus.disabled
            changed += 1
        db.flush()
        # Forward fix: terminal service owns no service IPs (idempotent, guarded).
        try:
            from app.services.ip_lifecycle import (
                release_service_ips_for_subscription,
            )

            for subscription in subscriptions:
                if subscription.status == SubscriptionStatus.disabled:
                    release_service_ips_for_subscription(db, subscription)
        except Exception:
            logger.warning(
                "service-IP release on reseller disable failed for account %s",
                account.id,
                exc_info=True,
            )
        compute_account_status(db, str(account.id))

    if not subscriptions:
        if normalized_action == "deactivate":
            account.status = SubscriberStatus.blocked
            account.is_active = True
        elif normalized_action == "restore":
            account.status = SubscriberStatus.active
            account.is_active = True
        else:
            account.status = SubscriberStatus.disabled
            account.is_active = False
        changed = 1
        db.flush()

    db.commit()
    db.refresh(account)
    return {
        "account_id": str(account.id),
        "status": account.status.value if account.status else None,
        "changed": changed,
        "skipped": skipped,
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
    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
        return None

    invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == account.id)
        .filter(Invoice.is_active.is_(True))
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
    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
        return None

    invoice = db.get(Invoice, coerce_uuid(invoice_id))
    if (
        not invoice
        or not invoice.is_active
        or str(invoice.account_id) != str(account.id)
    ):
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

    from app.services import billing as billing_service

    reseller_uuid = coerce_uuid(reseller_id)
    currency = billing_service.billing_accounts.get_for_reseller(
        db, reseller_id
    ).currency

    # Total revenue (all paid invoices)
    total_paid = (
        db.query(func.coalesce(func.sum(Invoice.total), 0))
        .join(Subscriber, Invoice.account_id == Subscriber.id)
        .filter(Subscriber.reseller_id == reseller_uuid)
        .filter(_customer_account_join_filter())
        .filter(Invoice.is_active.is_(True))
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
        .filter(_customer_account_join_filter())
        .filter(Invoice.is_active.is_(True))
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
        .filter(_customer_account_join_filter())
        .filter(Invoice.is_active.is_(True))
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
        _customer_accounts_query(db, reseller_id)
        .with_entities(func.count(Subscriber.id))
        .scalar()
    ) or 0

    return {
        "total_paid": total_paid,
        "total_outstanding": total_outstanding,
        "currency": currency,
        "account_count": account_count,
        "monthly": monthly,
    }


def get_profile(db: Session, reseller_id: str, subscriber_id: str) -> dict | None:
    """Org profile + MFA state for the reseller portal (web and bearer)."""
    from app.models.auth import MFAMethod
    from app.models.subscriber import Reseller

    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        return None
    methods = (
        db.query(MFAMethod)
        .filter(MFAMethod.subscriber_id == coerce_uuid(subscriber_id))
        .filter(MFAMethod.is_active.is_(True))
        .order_by(MFAMethod.created_at.desc())
        .all()
    )
    return {
        "name": reseller.name,
        "code": reseller.code,
        "contact_email": reseller.contact_email,
        "contact_phone": reseller.contact_phone,
        "notes": reseller.notes,
        "mfa_enabled": any(m.enabled and m.verified_at is not None for m in methods),
        "mfa_methods": [
            {
                "id": str(m.id),
                "label": m.label,
                "method_type": m.method_type.value,
                "verified_at": m.verified_at,
                "enabled": m.enabled,
            }
            for m in methods
        ],
    }


def update_profile(
    db: Session,
    reseller_id: str,
    subscriber_id: str,
    *,
    fields: dict,
) -> dict | None:
    """Apply contact-detail updates (present keys only; blank clears)."""
    from app.models.subscriber import Reseller

    reseller = db.get(Reseller, coerce_uuid(reseller_id))
    if reseller is None:
        return None
    for key in ("contact_email", "contact_phone", "notes"):
        if key in fields:
            setattr(reseller, key, (fields[key] or "").strip() or None)
    db.commit()
    return get_profile(db, reseller_id, subscriber_id)


def create_customer_impersonation_token(
    db: Session,
    reseller_id: str,
    account_id: str,
    *,
    acting_subscriber_id: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> dict:
    """Bearer counterpart of the web "view as": a 15-minute, read-only,
    customer-scoped access token + an audit trail of who impersonated whom."""
    import hashlib
    import secrets
    from datetime import timedelta

    from app.models.audit import AuditActorType, AuditEvent
    from app.models.auth import Session as AuthSession
    from app.models.auth import SessionStatus
    from app.services import auth_flow as auth_flow_service

    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber account not found"
        )

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=15)
    session = AuthSession(
        subscriber_id=account.id,
        status=SessionStatus.active,
        token_hash=hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest(),
        ip_address=ip_address,
        user_agent=f"[reseller-impersonation by {acting_subscriber_id}]",
        expires_at=expires_at,
    )
    db.add(session)
    db.add(
        AuditEvent(
            actor_type=AuditActorType.user,
            actor_id=str(acting_subscriber_id),
            action="reseller_impersonate",
            entity_type="subscriber",
            entity_id=str(account.id),
            status_code=200,
            is_success=True,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    db.commit()
    db.refresh(session)

    token = auth_flow_service.issue_impersonation_access_token(
        db, str(account.id), str(session.id), str(acting_subscriber_id)
    )
    return {
        "access_token": token,
        "expires_at": expires_at,
        "account_id": str(account.id),
        "customer_name": f"{account.first_name or ''} {account.last_name or ''}".strip()
        or account.email
        or "Customer",
    }


def create_customer_impersonation_session(
    db: Session,
    reseller_id: str,
    account_id: str,
    return_to: str,
) -> str:
    account = _get_customer_account(db, reseller_id, account_id)
    if not account:
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
        username=f"impersonate:reseller:{reseller_id}:{account.id}",
        account_id=account.id,
        subscriber_id=account.id,
        subscription_id=selected_subscription_id,
        is_impersonation=True,
        # Reseller "view as customer" uses the same customer portal capabilities
        # as the customer. The impersonation marker remains for attribution and
        # the Exit View banner.
        read_only=False,
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


# Backwards-compat alias for a historical bad auto-rename ("impersonation" ->
# "imsubscriberation"). Retained so any external callers keep working; prefer
# create_customer_impersonation_session.
create_customer_imsubscriberation_session = create_customer_impersonation_session
