"""Mutation helpers for admin system user management routes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.auth import Session as AuthSession
from app.models.rbac import SubscriberPermission as SubscriberPermissionModel
from app.models.rbac import SubscriberRole as SubscriberRoleModel
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.auth import UserCredentialCreate
from app.schemas.rbac import SubscriberRoleCreate
from app.services import auth as auth_service
from app.services import rbac as rbac_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid


def set_user_active(db: Session, *, user_id: str, is_active: bool) -> Subscriber:
    """Activate/deactivate subscriber and linked credentials."""
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")
    subscriber.is_active = is_active
    subscriber.status = SubscriberStatus.active if is_active else SubscriberStatus.suspended
    db.query(UserCredential).filter(UserCredential.subscriber_id == subscriber.id).update(
        {"is_active": is_active}
    )
    db.commit()
    return subscriber


def disable_user_mfa(db: Session, *, user_id: str) -> None:
    """Disable all MFA methods for a subscriber."""
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")
    db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).update(
        {"enabled": False, "is_active": False}
    )
    db.commit()


def reset_user_password(db: Session, *, user_id: str) -> str:
    """Reset active credential password and require change at next login."""
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")
    temp_password = secrets.token_urlsafe(16)
    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id,
        UserCredential.is_active.is_(True),
    ).update(
        {
            "password_hash": hash_password(temp_password),
            "must_change_password": True,
            "password_updated_at": datetime.now(UTC),
        }
    )
    db.commit()
    return temp_password


def create_user_with_role_and_password(
    db: Session,
    *,
    first_name: str,
    last_name: str,
    email: str,
    role_id: str,
) -> tuple[Subscriber, str]:
    """Create subscriber, assign role, and generate temp credential password."""
    role = rbac_service.roles.get(db, role_id)

    subscriber = Subscriber(
        first_name=first_name,
        last_name=last_name,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
    )
    db.add(subscriber)
    db.flush()

    rbac_service.subscriber_roles.create(
        db,
        SubscriberRoleCreate(subscriber_id=subscriber.id, role_id=role.id),
    )

    temp_password = secrets.token_urlsafe(16)
    auth_service.user_credentials.create(
        db,
        UserCredentialCreate(
            subscriber_id=subscriber.id,
            username=email,
            password_hash=hash_password(temp_password),
            must_change_password=True,
        ),
    )
    db.commit()
    return subscriber, temp_password


def send_user_invite(db: Session, *, email: str) -> str:
    """Send invitation email to a newly created user.

    Returns a status note describing the outcome.
    """
    from app.services import auth_flow as auth_flow_service
    from app.services import email as email_service

    reset = auth_flow_service.request_password_reset(db=db, email=email)
    if not reset or not reset.get("token"):
        return "User created, but no reset token was generated."

    sent = email_service.send_user_invite_email(
        db,
        to_email=email,
        reset_token=reset["token"],
        person_name=reset.get("subscriber_name"),
    )
    if sent:
        return "Invitation sent. Password reset email delivered."
    return "User created, but the reset email could not be sent."


def delete_user_records(db: Session, *, user_id: str) -> Subscriber:
    """Delete subscriber and linked auth/RBAC rows."""
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")

    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(MFAMethod).filter(
        MFAMethod.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(AuthSession).filter(
        AuthSession.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(ApiKey).filter(
        ApiKey.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(SubscriberRoleModel).filter(
        SubscriberRoleModel.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(SubscriberPermissionModel).filter(
        SubscriberPermissionModel.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.delete(subscriber)
    db.commit()
    return subscriber
