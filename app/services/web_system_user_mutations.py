"""Mutation helpers for admin system user management routes."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.auth import ApiKey, AuthProvider, MFAMethod, UserCredential
from app.models.auth import Session as AuthSession
from app.models.rbac import (
    SystemUserPermission as SystemUserPermissionModel,
)
from app.models.rbac import SystemUserRole as SystemUserRoleModel
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import rbac as rbac_service
from app.services import web_system_users as web_system_users_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid


def _invite_login_route_for_user(_system_user: SystemUser) -> str:
    return "/auth/login?next=/admin/dashboard"


def set_user_active(db: Session, *, user_id: str, is_active: bool) -> SystemUser:
    """Activate/deactivate system user and linked credentials."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    system_user.is_active = is_active
    db.query(UserCredential).filter(UserCredential.system_user_id == system_user.id).update(
        {"is_active": is_active}
    )
    db.commit()
    return system_user


def disable_user_mfa(db: Session, *, user_id: str) -> None:
    """Disable all MFA methods for a system user."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    db.query(MFAMethod).filter(MFAMethod.system_user_id == system_user.id).update(
        {"enabled": False, "is_active": False}
    )
    db.commit()


def reset_user_password(db: Session, *, user_id: str) -> str:
    """Reset active credential password and require change at next login."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    temp_password = secrets.token_urlsafe(16)
    db.query(UserCredential).filter(
        UserCredential.system_user_id == system_user.id,
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
) -> tuple[SystemUser, str]:
    """Create system user, assign role, and generate temp credential password."""
    role = rbac_service.roles.get(db, role_id)

    system_user = SystemUser(
        first_name=first_name,
        last_name=last_name,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
        user_type=UserType.system_user,
    )
    db.add(system_user)
    db.flush()

    db.add(SystemUserRoleModel(system_user_id=system_user.id, role_id=role.id))

    temp_password = secrets.token_urlsafe(16)
    db.add(
        UserCredential(
            system_user_id=system_user.id,
            provider=AuthProvider.local,
            username=email,
            password_hash=hash_password(temp_password),
            must_change_password=True,
        )
    )
    db.commit()
    return system_user, temp_password


def bulk_set_user_type(
    db: Session,
    *,
    user_ids: list[str],
    user_type: str,
) -> int:
    """Bulk update user type for selected system users."""
    if not user_ids:
        return 0
    normalized_type = web_system_users_service.normalize_user_type(user_type)
    uuids = [coerce_uuid(user_id) for user_id in user_ids]
    updated = (
        db.query(SystemUser)
        .filter(SystemUser.id.in_(uuids))
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
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    if not system_user.email:
        raise ValueError("User has no email address")
    _ensure_local_credential(db, system_user)
    next_login_path = _invite_login_route_for_user(system_user)
    return send_user_invite(
        db,
        email=system_user.email,
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

    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    if not system_user.email:
        raise ValueError("User has no email address")
    _ensure_local_credential(db, system_user)

    reset = auth_flow_service.request_password_reset(db=db, email=system_user.email)
    if not reset or not reset.get("token"):
        return "Password reset link could not be generated for this user."

    sent = email_service.send_password_reset_email(
        db,
        to_email=system_user.email,
        reset_token=reset["token"],
        person_name=reset.get("subscriber_name"),
    )
    if sent:
        return "Password reset link sent successfully."
    return "Password reset link could not be sent."


def _ensure_local_credential(db: Session, system_user: SystemUser) -> None:
    """Ensure the system user has an active local credential for reset-link flow."""
    credential = (
        db.query(UserCredential)
        .filter(UserCredential.system_user_id == system_user.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .order_by(UserCredential.created_at.desc())
        .first()
    )

    generated_password_hash = hash_password(secrets.token_urlsafe(24))
    if credential:
        if not credential.username:
            credential.username = system_user.email
        if not credential.password_hash:
            credential.password_hash = generated_password_hash
        credential.is_active = True
        credential.must_change_password = True
        credential.password_updated_at = datetime.now(UTC)
        db.flush()
        return

    db.add(
        UserCredential(
            system_user_id=system_user.id,
            provider=AuthProvider.local,
            username=system_user.email,
            password_hash=generated_password_hash,
            must_change_password=True,
            password_updated_at=datetime.now(UTC),
            is_active=True,
        )
    )
    db.flush()


def ensure_local_credential_for_user(db: Session, *, user_id: str) -> None:
    """Ensure an existing system user has an active local credential."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    if not system_user.email:
        raise ValueError("User has no email address")
    _ensure_local_credential(db, system_user)
    db.commit()


def set_local_login_active(db: Session, *, user_id: str, is_active: bool) -> None:
    """Activate/deactivate local login credential for a system user."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")
    if is_active:
        _ensure_local_credential(db, system_user)
        db.query(UserCredential).filter(
            UserCredential.system_user_id == system_user.id,
            UserCredential.provider == AuthProvider.local,
        ).update({"is_active": True}, synchronize_session=False)
    else:
        db.query(UserCredential).filter(
            UserCredential.system_user_id == system_user.id,
            UserCredential.provider == AuthProvider.local,
        ).update({"is_active": False}, synchronize_session=False)
    db.commit()


def delete_user_records(db: Session, *, user_id: str) -> SystemUser:
    """Delete system user and linked auth/RBAC rows."""
    system_user = db.get(SystemUser, coerce_uuid(user_id))
    if not system_user:
        raise ValueError("User not found")

    db.query(UserCredential).filter(
        UserCredential.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(MFAMethod).filter(
        MFAMethod.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(AuthSession).filter(
        AuthSession.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(ApiKey).filter(
        ApiKey.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(SystemUserRoleModel).filter(
        SystemUserRoleModel.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.query(SystemUserPermissionModel).filter(
        SystemUserPermissionModel.system_user_id == system_user.id
    ).delete(synchronize_session=False)
    db.delete(system_user)
    db.commit()
    return system_user


def bulk_delete_user_records(db: Session, *, user_ids: list[str]) -> tuple[int, int]:
    """Delete inactive system users that have no linked records.

    Returns (deleted_count, skipped_count).
    """
    deleted_count = 0
    skipped_count = 0

    for user_id in user_ids:
        system_user = db.get(SystemUser, coerce_uuid(user_id))
        if not system_user or system_user.is_active:
            skipped_count += 1
            continue

        db.query(UserCredential).filter(
            UserCredential.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(MFAMethod).filter(
            MFAMethod.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(AuthSession).filter(
            AuthSession.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(ApiKey).filter(
            ApiKey.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(SystemUserRoleModel).filter(
            SystemUserRoleModel.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.query(SystemUserPermissionModel).filter(
            SystemUserPermissionModel.system_user_id == system_user.id
        ).delete(synchronize_session=False)
        db.delete(system_user)
        deleted_count += 1

    db.commit()
    return deleted_count, skipped_count
