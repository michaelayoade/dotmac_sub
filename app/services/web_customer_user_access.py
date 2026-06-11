"""Customer-detail user access helpers (invite/reset/credential state)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, UserCredential
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber, SubscriberCategory
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services.audit_adapter import record_audit_event
from app.services.rate_limiter_adapter import allow_operation
from app.services.settings_spec import resolve_value
from app.timezone import APP_TIMEZONE_NAME, format_in_app_timezone

logger = logging.getLogger(__name__)

INVITE_AUDIT_ACTION = "customer_user_invite"
RESET_AUDIT_ACTION = "customer_user_reset_link"
LOGIN_TOGGLE_AUDIT_ACTION = "customer_user_login_toggle"
PRIMARY_LOGIN_AUDIT_ACTION = "customer_user_primary_login_set"


@dataclass
class CustomerUserTarget:
    subscriber: Subscriber
    email: str
    source: str


def _now() -> datetime:
    return datetime.now(UTC)


# Where a customer lands after accepting an invite / completing a reset.
CUSTOMER_LOGIN_NEXT = "/portal/auth/login?next=/portal/dashboard"


def _ensure_subscriber_local_credential(
    db: Session, subscriber: Subscriber
) -> UserCredential:
    """Create or reactivate a customer's local portal credential.

    Customer portal credentials live in ``UserCredential`` keyed by
    ``subscriber_id``. The staff helpers in ``web_system_user_mutations`` key on
    ``system_user_id`` (via ``db.get(SystemUser, ...)``) and never match a
    customer — which is why customer invite/reset/activate failed with
    "User not found". This mirrors ``_ensure_local_credential`` on the subscriber
    side. Local credentials require username + password_hash (DB CHECK); the
    random hash is a placeholder the customer replaces via the invite/reset link.
    The customer portal accepts this local credential OR RADIUS at login.
    """
    import secrets

    from app.services.auth_flow import hash_password

    cred = (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
        .first()
    )
    if cred:
        if not cred.username:
            cred.username = subscriber.email
        if not cred.password_hash:
            cred.password_hash = hash_password(secrets.token_urlsafe(24))
        cred.is_active = True
        cred.must_change_password = True
        cred.password_updated_at = _now()
        db.flush()
        return cred
    cred = UserCredential(
        subscriber_id=subscriber.id,
        provider=AuthProvider.local,
        username=subscriber.email,
        password_hash=hash_password(secrets.token_urlsafe(24)),
        must_change_password=True,
        password_updated_at=_now(),
        is_active=True,
    )
    db.add(cred)
    db.flush()
    return cred


def _set_subscriber_local_login_active(
    db: Session, subscriber: Subscriber, *, is_active: bool
) -> None:
    """Activate/deactivate a customer's local portal login (subscriber-keyed)."""
    if is_active:
        _ensure_subscriber_local_credential(db, subscriber)
    else:
        db.query(UserCredential).filter(
            UserCredential.subscriber_id == subscriber.id,
            UserCredential.provider == AuthProvider.local,
        ).update({"is_active": False}, synchronize_session=False)
    db.commit()


def _send_subscriber_reset_link(
    db: Session, *, subscriber: Subscriber, email: str
) -> str:
    """Send a portal password-reset link to a customer (subscriber-keyed)."""
    from app.services import auth_flow as auth_flow_service
    from app.services import email as email_service

    _ensure_subscriber_local_credential(db, subscriber)
    db.commit()
    reset = auth_flow_service.request_password_reset(db=db, email=email)
    if not reset or not reset.get("token"):
        return "Password reset link could not be generated for this user."
    sent = email_service.send_password_reset_email(
        db,
        to_email=email,
        reset_token=reset["token"],
        person_name=reset.get("subscriber_name"),
        next_login_path=CUSTOMER_LOGIN_NEXT,
    )
    return (
        "Password reset link sent successfully."
        if sent
        else "Password reset link could not be sent."
    )


def _invite_expiry_minutes(db: Session) -> int:
    value = resolve_value(db, SettingDomain.auth, "user_invite_expiry_minutes") or 1440
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 1440
    return parsed if parsed > 0 else 1440


def _resolve_business_primary_contact(
    db: Session, subscriber_id: str
) -> Subscriber | None:
    subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not subscriber or subscriber.category != SubscriberCategory.business:
        return None
    if (subscriber.email or "").strip():
        return subscriber
    return None


def resolve_customer_user_target(
    db: Session, *, customer_type: str, customer_id: str
) -> CustomerUserTarget:
    if customer_type == "person":
        subscriber = db.get(Subscriber, UUID(str(customer_id)))
        if not subscriber:
            raise ValueError("Customer not found")
        email = (subscriber.email or "").strip()
        if not email:
            raise ValueError("Customer has no email address")
        return CustomerUserTarget(
            subscriber=subscriber, email=email, source="subscriber_email"
        )

    if customer_type == "business":
        primary = _resolve_business_primary_contact(db, customer_id)
        if not primary:
            raise ValueError("Business customer has no primary contact with email")
        email = (primary.email or "").strip()
        if not email:
            raise ValueError("Business customer primary contact has no email")
        return CustomerUserTarget(
            subscriber=primary, email=email, source="primary_contact_email"
        )

    raise ValueError("Unsupported customer type")


def resolve_subscriber_user_target(
    db: Session, *, subscriber_id: str
) -> CustomerUserTarget:
    subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not subscriber:
        raise ValueError("Subscriber not found")

    if subscriber.category == SubscriberCategory.business:
        primary = _resolve_business_primary_contact(db, str(subscriber.id))
        if primary and (primary.email or "").strip():
            return CustomerUserTarget(
                subscriber=primary,
                email=(primary.email or "").strip(),
                source="primary_contact_email",
            )

    email = (subscriber.email or "").strip()
    if email:
        return CustomerUserTarget(
            subscriber=subscriber,
            email=email,
            source="subscriber_email",
        )

    raise ValueError("Subscriber has no email address")


def _latest_local_credential(db: Session, subscriber_id: str) -> UserCredential | None:
    return (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id == UUID(str(subscriber_id)))
        .filter(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
        .first()
    )


def _last_success_audit(
    db: Session, *, action: str, subscriber_id: str
) -> AuditEvent | None:
    return (
        db.query(AuditEvent)
        .filter(AuditEvent.action == action)
        .filter(AuditEvent.entity_type == "subscriber")
        .filter(AuditEvent.entity_id == str(subscriber_id))
        .filter(AuditEvent.is_success.is_(True))
        .order_by(AuditEvent.occurred_at.desc())
        .first()
    )


def _count_success_since(
    db: Session, *, action: str, subscriber_id: str, since: datetime
) -> int:
    return int(
        db.query(func.count(AuditEvent.id))
        .filter(AuditEvent.action == action)
        .filter(AuditEvent.entity_type == "subscriber")
        .filter(AuditEvent.entity_id == str(subscriber_id))
        .filter(AuditEvent.is_success.is_(True))
        .filter(AuditEvent.occurred_at >= since)
        .scalar()
        or 0
    )


def build_customer_user_access_state(
    db: Session, *, customer_type: str, customer_id: str
) -> dict:
    target = resolve_customer_user_target(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    return _build_user_access_state(db, target=target)


def build_subscriber_user_access_state(db: Session, *, subscriber_id: str) -> dict:
    page_subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not page_subscriber:
        raise ValueError("Subscriber not found")
    target = resolve_subscriber_user_target(db, subscriber_id=subscriber_id)
    return _build_user_access_state(db, target=target, page_subscriber=page_subscriber)


def _build_user_access_state(
    db: Session,
    *,
    target: CustomerUserTarget,
    page_subscriber: Subscriber | None = None,
) -> dict:
    credential = _latest_local_credential(db, str(target.subscriber.id))
    page = page_subscriber or target.subscriber
    primary = (
        _resolve_business_primary_contact(db, str(page.id))
        if page.category == SubscriberCategory.business
        else None
    )

    last_invite = _last_success_audit(
        db, action=INVITE_AUDIT_ACTION, subscriber_id=str(target.subscriber.id)
    )
    invite_expiry_minutes = _invite_expiry_minutes(db)
    invite_available_at = None
    if last_invite and last_invite.occurred_at:
        invite_available_at = last_invite.occurred_at + timedelta(
            minutes=invite_expiry_minutes
        )

    now = _now()
    reset_since = now - timedelta(hours=1)
    resets_last_hour = _count_success_since(
        db,
        action=RESET_AUDIT_ACTION,
        subscriber_id=str(target.subscriber.id),
        since=reset_since,
    )
    reset_remaining = max(0, 3 - resets_last_hour)

    return {
        "target_subscriber_id": str(target.subscriber.id),
        "target_subscriber_name": target.subscriber.display_name
        or f"{target.subscriber.first_name} {target.subscriber.last_name}".strip(),
        "page_subscriber_id": str(page.id),
        "business_account_id": str(page.id)
        if page.category == SubscriberCategory.business
        else None,
        "primary_login_subscriber_id": str(primary.id) if primary else None,
        "primary_login_subscriber_name": (
            primary.display_name or f"{primary.first_name} {primary.last_name}".strip()
            if primary
            else None
        ),
        "is_primary_login_subscriber": bool(primary and primary.id == page.id),
        "can_set_primary_login": bool(
            page.category == SubscriberCategory.business and (page.email or "").strip()
        ),
        "email": target.email,
        "email_source": target.source,
        "has_credential": credential is not None,
        "login_active": bool(credential and credential.is_active),
        "must_change_password": bool(credential and credential.must_change_password),
        "last_login_at": credential.last_login_at if credential else None,
        "invite_sent_at": last_invite.occurred_at if last_invite else None,
        "invite_expiry_minutes": invite_expiry_minutes,
        "invite_available_at": invite_available_at,
        "can_send_invite": invite_available_at is None or now >= invite_available_at,
        "resets_last_hour": resets_last_hour,
        "reset_remaining": reset_remaining,
        "can_send_reset": reset_remaining > 0,
    }


def activate_customer_login(
    db: Session, *, customer_type: str, customer_id: str
) -> CustomerUserTarget:
    target = resolve_customer_user_target(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    _set_subscriber_local_login_active(db, target.subscriber, is_active=True)
    return target


def send_customer_invite(
    db: Session,
    *,
    request,
    customer_type: str,
    customer_id: str,
    actor_id: str | None,
) -> dict[str, object]:
    """Send or reject a customer portal invite and record audit metadata."""
    state = build_customer_user_access_state(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    if not state.get("can_send_invite"):
        retry_at = state.get("invite_available_at")
        when = (
            f"{format_in_app_timezone(retry_at, '%Y-%m-%d %H:%M')} {APP_TIMEZONE_NAME}"
            if retry_at
            else "later"
        )
        record_audit_event(
            db,
            action=INVITE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state.get("target_subscriber_id") or ""),
            actor_id=actor_id,
            metadata={"reason": "rate_limited"},
            status_code=429,
            is_success=False,
        )
        return {
            "ok": False,
            "title": "Invite blocked",
            "message": f"Invite already sent recently. You can resend after {when}.",
        }
    decision = allow_operation(
        f"customer_invite:{state['target_subscriber_id']}",
        limit=1,
        window_seconds=int(state.get("invite_expiry_minutes") or 60) * 60,
    )
    if not decision.allowed:
        record_audit_event(
            db,
            action=INVITE_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state.get("target_subscriber_id") or ""),
            actor_id=actor_id,
            metadata={"reason": "rate_limited_adapter"},
            status_code=429,
            is_success=False,
        )
        return {
            "ok": False,
            "title": "Invite blocked",
            "message": "Invite rate limit reached. Try again later.",
        }

    target = resolve_customer_user_target(
        db, customer_type=customer_type, customer_id=customer_id
    )
    _ensure_subscriber_local_credential(db, target.subscriber)
    db.commit()
    note = web_system_user_mutations_service.send_user_invite(
        db,
        email=target.email,
        next_login_path=CUSTOMER_LOGIN_NEXT,
    )
    ok = "sent" in note.lower()
    record_audit_event(
        db,
        action=INVITE_AUDIT_ACTION,
        entity_type="subscriber",
        entity_id=str(state["target_subscriber_id"]),
        actor_id=actor_id,
        metadata={
            "email": state.get("email"),
            "email_source": state.get("email_source"),
            "customer_type": customer_type,
            "result": note,
        },
        status_code=200,
        is_success=ok,
    )
    return {"ok": ok, "title": "User invite", "message": note}


def send_customer_reset_link(
    db: Session,
    *,
    request,
    customer_type: str,
    customer_id: str,
    actor_id: str | None,
) -> dict[str, object]:
    """Send or reject a customer password reset link and record audit metadata."""
    state = build_customer_user_access_state(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    if not state.get("can_send_reset"):
        record_audit_event(
            db,
            action=RESET_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state.get("target_subscriber_id") or ""),
            actor_id=actor_id,
            metadata={"reason": "rate_limited"},
            status_code=429,
            is_success=False,
        )
        return {
            "ok": False,
            "title": "Reset link blocked",
            "message": "Reset limit reached: max 3 reset links per hour.",
        }
    decision = allow_operation(
        f"customer_reset:{state['target_subscriber_id']}",
        limit=3,
        window_seconds=3600,
    )
    if not decision.allowed:
        record_audit_event(
            db,
            action=RESET_AUDIT_ACTION,
            entity_type="subscriber",
            entity_id=str(state.get("target_subscriber_id") or ""),
            actor_id=actor_id,
            metadata={"reason": "rate_limited_adapter"},
            status_code=429,
            is_success=False,
        )
        return {
            "ok": False,
            "title": "Reset link blocked",
            "message": "Reset limit reached: max 3 reset links per hour.",
        }

    target = resolve_customer_user_target(
        db, customer_type=customer_type, customer_id=customer_id
    )
    note = _send_subscriber_reset_link(
        db, subscriber=target.subscriber, email=target.email
    )
    ok = "sent" in note.lower()
    record_audit_event(
        db,
        action=RESET_AUDIT_ACTION,
        entity_type="subscriber",
        entity_id=str(state["target_subscriber_id"]),
        actor_id=actor_id,
        metadata={
            "email": state.get("email"),
            "email_source": state.get("email_source"),
            "customer_type": customer_type,
            "result": note,
        },
        status_code=200,
        is_success=ok,
    )
    return {"ok": ok, "title": "Password reset", "message": note}


def set_customer_login_active(
    db: Session,
    *,
    request,
    customer_type: str,
    customer_id: str,
    actor_id: str | None,
    is_active: bool,
) -> dict[str, object]:
    """Toggle customer portal login and record audit metadata."""
    target = (
        activate_customer_login(
            db, customer_type=customer_type, customer_id=customer_id
        )
        if is_active
        else deactivate_customer_login(
            db, customer_type=customer_type, customer_id=customer_id
        )
    )
    record_audit_event(
        db,
        action=LOGIN_TOGGLE_AUDIT_ACTION,
        entity_type="subscriber",
        entity_id=str(target.subscriber.id),
        actor_id=actor_id,
        metadata={"login_active": is_active, "customer_type": customer_type},
    )
    return {
        "ok": True,
        "title": "Login activated" if is_active else "Login deactivated",
        "message": "Customer portal login has been activated."
        if is_active
        else "Customer portal login has been deactivated.",
    }


def log_customer_user_access_error(
    db: Session,
    *,
    request,
    action: str,
    customer_type: str,
    customer_id: str,
    actor_id: str | None,
    error: Exception,
    login_active: bool | None = None,
) -> None:
    metadata = {"customer_type": customer_type, "error": str(error)}
    if login_active is not None:
        metadata["login_active"] = login_active
    record_audit_event(
        db,
        action=action,
        entity_type="customer",
        entity_id=str(customer_id),
        actor_id=actor_id,
        metadata=metadata,
        status_code=500,
        is_success=False,
    )


def activate_subscriber_login(db: Session, *, subscriber_id: str) -> CustomerUserTarget:
    target = resolve_subscriber_user_target(db, subscriber_id=subscriber_id)
    _set_subscriber_local_login_active(db, target.subscriber, is_active=True)
    return target


def deactivate_customer_login(
    db: Session, *, customer_type: str, customer_id: str
) -> CustomerUserTarget:
    target = resolve_customer_user_target(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    _set_subscriber_local_login_active(db, target.subscriber, is_active=False)
    return target


def deactivate_subscriber_login(
    db: Session, *, subscriber_id: str
) -> CustomerUserTarget:
    target = resolve_subscriber_user_target(db, subscriber_id=subscriber_id)
    _set_subscriber_local_login_active(db, target.subscriber, is_active=False)
    return target


def set_org_primary_login_subscriber(
    db: Session,
    *,
    subscriber_id: str,
) -> CustomerUserTarget:
    subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not subscriber:
        raise ValueError("Subscriber not found")
    if subscriber.category != SubscriberCategory.business:
        raise ValueError("Subscriber is not a business customer")
    email = (subscriber.email or "").strip()
    if not email:
        raise ValueError("Subscriber needs an email before becoming primary login")
    db.commit()

    return CustomerUserTarget(
        subscriber=subscriber,
        email=email,
        source="business_primary_login",
    )
