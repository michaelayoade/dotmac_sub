"""Customer-detail user access helpers (invite/reset/credential state)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.auth import AuthProvider, UserCredential
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Organization, Subscriber, UserType
from app.services import web_system_user_mutations as web_system_user_mutations_service
from app.services.settings_spec import resolve_value


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


def _invite_expiry_minutes(db: Session) -> int:
    value = resolve_value(db, SettingDomain.auth, "user_invite_expiry_minutes") or 60
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 60
    return parsed if parsed > 0 else 60


def _resolve_org_primary_contact(db: Session, organization_id: str) -> Subscriber | None:
    org_uuid = UUID(str(organization_id))
    organization = db.get(Organization, org_uuid)
    if organization and organization.primary_login_subscriber_id:
        configured = db.get(Subscriber, organization.primary_login_subscriber_id)
        if (
            configured
            and configured.organization_id == org_uuid
            and bool((configured.email or "").strip())
        ):
            return configured

    # Best-effort "primary" resolution:
    # 1) active customers with email, newest first
    # 2) any customers with email, newest first
    active = (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .filter(Subscriber.user_type == UserType.customer)
        .filter(Subscriber.is_active.is_(True))
        .filter(Subscriber.email.isnot(None))
        .order_by(Subscriber.updated_at.desc(), Subscriber.created_at.desc())
        .first()
    )
    if active:
        return active
    fallback = (
        db.query(Subscriber)
        .filter(Subscriber.organization_id == org_uuid)
        .filter(Subscriber.user_type == UserType.customer)
        .filter(Subscriber.email.isnot(None))
        .order_by(Subscriber.updated_at.desc(), Subscriber.created_at.desc())
        .first()
    )
    return fallback


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
        return CustomerUserTarget(subscriber=subscriber, email=email, source="subscriber_email")

    if customer_type == "organization":
        primary = _resolve_org_primary_contact(db, customer_id)
        if not primary:
            raise ValueError("Organization has no primary contact with email")
        email = (primary.email or "").strip()
        if not email:
            raise ValueError("Organization primary contact has no email")
        return CustomerUserTarget(subscriber=primary, email=email, source="primary_contact_email")

    raise ValueError("Unsupported customer type")


def resolve_subscriber_user_target(
    db: Session, *, subscriber_id: str
) -> CustomerUserTarget:
    subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not subscriber:
        raise ValueError("Subscriber not found")

    if subscriber.organization_id:
        primary = _resolve_org_primary_contact(db, str(subscriber.organization_id))
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
        _resolve_org_primary_contact(db, str(page.organization_id))
        if page.organization_id
        else None
    )

    last_invite = _last_success_audit(
        db, action=INVITE_AUDIT_ACTION, subscriber_id=str(target.subscriber.id)
    )
    invite_expiry_minutes = _invite_expiry_minutes(db)
    invite_available_at = None
    if last_invite and last_invite.occurred_at:
        invite_available_at = last_invite.occurred_at + timedelta(minutes=invite_expiry_minutes)

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
        "organization_id": str(page.organization_id) if page.organization_id else None,
        "primary_login_subscriber_id": str(primary.id) if primary else None,
        "primary_login_subscriber_name": (
            primary.display_name
            or f"{primary.first_name} {primary.last_name}".strip()
            if primary
            else None
        ),
        "is_primary_login_subscriber": bool(primary and primary.id == page.id),
        "can_set_primary_login": bool(page.organization_id and (page.email or "").strip()),
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
    web_system_user_mutations_service.set_local_login_active(
        db,
        user_id=str(target.subscriber.id),
        is_active=True,
    )
    return target


def activate_subscriber_login(db: Session, *, subscriber_id: str) -> CustomerUserTarget:
    target = resolve_subscriber_user_target(db, subscriber_id=subscriber_id)
    web_system_user_mutations_service.set_local_login_active(
        db,
        user_id=str(target.subscriber.id),
        is_active=True,
    )
    return target


def deactivate_customer_login(
    db: Session, *, customer_type: str, customer_id: str
) -> CustomerUserTarget:
    target = resolve_customer_user_target(
        db,
        customer_type=customer_type,
        customer_id=customer_id,
    )
    web_system_user_mutations_service.set_local_login_active(
        db,
        user_id=str(target.subscriber.id),
        is_active=False,
    )
    return target


def deactivate_subscriber_login(db: Session, *, subscriber_id: str) -> CustomerUserTarget:
    target = resolve_subscriber_user_target(db, subscriber_id=subscriber_id)
    web_system_user_mutations_service.set_local_login_active(
        db,
        user_id=str(target.subscriber.id),
        is_active=False,
    )
    return target


def set_org_primary_login_subscriber(
    db: Session,
    *,
    subscriber_id: str,
) -> CustomerUserTarget:
    subscriber = db.get(Subscriber, UUID(str(subscriber_id)))
    if not subscriber:
        raise ValueError("Subscriber not found")
    if not subscriber.organization_id:
        raise ValueError("Subscriber is not linked to an organization")
    email = (subscriber.email or "").strip()
    if not email:
        raise ValueError("Subscriber needs an email before becoming primary login")

    organization = db.get(Organization, subscriber.organization_id)
    if not organization:
        raise ValueError("Organization not found")

    organization.primary_login_subscriber_id = subscriber.id
    db.add(organization)
    db.commit()
    db.refresh(organization)

    return CustomerUserTarget(
        subscriber=subscriber,
        email=email,
        source="organization_primary_login",
    )
