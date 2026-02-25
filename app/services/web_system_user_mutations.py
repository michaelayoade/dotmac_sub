"""Mutation helpers for admin system user management routes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.auth import ApiKey, AuthProvider, MFAMethod, UserCredential
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
from app.services import web_system_users as web_system_users_service


def _invite_login_route_for_user(subscriber: Subscriber) -> str:
    user_type = subscriber.user_type.value if subscriber.user_type else "system_user"
    if user_type == "customer":
        return "/portal/auth/login?next=/portal/dashboard"
    if user_type == "reseller":
        return "/reseller/auth/login"
    return "/auth/login?next=/admin/dashboard"


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
    user_type: str | None = None,
) -> tuple[Subscriber, str]:
    """Create subscriber, assign role, and generate temp credential password."""
    role = rbac_service.roles.get(db, role_id)

    subscriber = Subscriber(
        first_name=first_name,
        last_name=last_name,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
        user_type=web_system_users_service.normalize_user_type(user_type),
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


def bulk_set_user_type(
    db: Session,
    *,
    user_ids: list[str],
    user_type: str,
) -> int:
    """Bulk update user type for selected subscribers."""
    if not user_ids:
        return 0
    normalized_type = web_system_users_service.normalize_user_type(user_type)
    uuids = [coerce_uuid(user_id) for user_id in user_ids]
    updated = (
        db.query(Subscriber)
        .filter(Subscriber.id.in_(uuids))
        .update({"user_type": normalized_type}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)


def send_user_invite(
    db: Session,
    *,
    email: str,
    next_login_path: str | None = None,
) -> str:
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
        next_login_path=next_login_path,
    )
    if sent:
        return "Invitation sent. Password reset email delivered."
    return "User created, but the reset email could not be sent."


def send_user_invite_for_user(db: Session, *, user_id: str) -> str:
    """Send invite email for an existing user."""
    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")
    if not subscriber.email:
        raise ValueError("User has no email address")
    _ensure_local_credential(db, subscriber)
    next_login_path = _invite_login_route_for_user(subscriber)
    return send_user_invite(
        db,
        email=subscriber.email,
        next_login_path=next_login_path,
    )


def bulk_send_user_invites(db: Session, *, user_ids: list[str]) -> tuple[int, int]:
    """Send welcome invites for selected users.

    Returns (sent_count, failed_count).
    """
    sent_count = 0
    failed_count = 0

    for user_id in user_ids:
        try:
            send_user_invite_for_user(db, user_id=user_id)
            sent_count += 1
        except Exception:
            failed_count += 1

    return sent_count, failed_count


def send_password_reset_link_for_user(db: Session, *, user_id: str) -> str:
    """Send password reset link email for an existing user."""
    from app.services import auth_flow as auth_flow_service
    from app.services import email as email_service

    subscriber = db.get(Subscriber, coerce_uuid(user_id))
    if not subscriber:
        raise ValueError("User not found")
    if not subscriber.email:
        raise ValueError("User has no email address")
    _ensure_local_credential(db, subscriber)

    reset = auth_flow_service.request_password_reset(db=db, email=subscriber.email)
    if not reset or not reset.get("token"):
        return "Password reset link could not be generated for this user."

    sent = email_service.send_password_reset_email(
        db,
        to_email=subscriber.email,
        reset_token=reset["token"],
        person_name=reset.get("subscriber_name"),
    )
    if sent:
        return "Password reset link sent successfully."
    return "Password reset link could not be sent."


def _ensure_local_credential(db: Session, subscriber: Subscriber) -> None:
    """Ensure the subscriber has an active local credential for reset-link flow."""
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.subscriber_id == subscriber.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
        .first()
    )

    generated_password_hash = hash_password(secrets.token_urlsafe(24))
    if credential:
        if not credential.username:
            credential.username = subscriber.email
        if not credential.password_hash:
            credential.password_hash = generated_password_hash
        credential.is_active = True
        credential.must_change_password = True
        credential.password_updated_at = datetime.now(UTC)
        db.flush()
        return

    db.add(
        UserCredential(
            subscriber_id=subscriber.id,
            provider=AuthProvider.local,
            username=subscriber.email,
            password_hash=generated_password_hash,
            must_change_password=True,
            password_updated_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db.flush()


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


def bulk_delete_user_records(db: Session, *, user_ids: list[str]) -> tuple[int, int]:
    """Delete inactive subscribers that have no linked records.

    Returns (deleted_count, skipped_count).
    """
    deleted_count = 0
    skipped_count = 0

    for user_id in user_ids:
        subscriber = db.get(Subscriber, coerce_uuid(user_id))
        if not subscriber or subscriber.is_active:
            skipped_count += 1
            continue

        from app.services import web_system_common as web_system_common_service
        linked = web_system_common_service.linked_user_labels(db, subscriber.id)
        if linked:
            skipped_count += 1
            continue

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
        deleted_count += 1

    db.commit()
    return deleted_count, skipped_count
