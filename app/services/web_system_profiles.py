"""Service helpers for admin system user profile/detail/edit pages."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.rbac import Permission, Role, SystemUserPermission, SystemUserRole
from app.models.system_user import SystemUser
from app.services.common import coerce_uuid


def get_subscriber(db: Session, user_id: str | UUID | None) -> SystemUser | None:
    """Resolve a system user by id."""
    if not user_id:
        return None
    try:
        return db.get(SystemUser, coerce_uuid(user_id))
    except (TypeError, ValueError):
        return None


def get_profile_data(db: Session, person_id: str | UUID | None) -> dict[str, Any]:
    """Return profile page data for the logged-in system user."""
    person = get_subscriber(db, person_id)
    if not person:
        return {
            "person": None,
            "credential": None,
            "mfa_enabled": False,
            "api_key_count": 0,
        }

    credential = db.execute(
        select(UserCredential)
        .where(UserCredential.system_user_id == person.id)
        .where(UserCredential.is_active.is_(True))
        .limit(1)
    ).scalars().first()

    mfa_enabled = bool(
        db.scalar(
            select(MFAMethod.id)
            .where(MFAMethod.system_user_id == person.id)
            .where(MFAMethod.enabled.is_(True))
            .limit(1)
        )
    )

    api_key_count = (
        db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.system_user_id == person.id)
            .where(ApiKey.is_active.is_(True))
            .where(ApiKey.revoked_at.is_(None))
        )
        or 0
    )

    return {
        "person": person,
        "credential": credential,
        "mfa_enabled": mfa_enabled,
        "api_key_count": api_key_count,
    }


def update_profile(
    db: Session,
    *,
    person: SystemUser,
    first_name: str | None,
    last_name: str | None,
    email: str | None,
    phone: str | None,
) -> SystemUser:
    """Apply profile updates and persist."""
    if first_name:
        person.first_name = first_name
    if last_name:
        person.last_name = last_name
    if email:
        person.email = email
    if phone:
        person.phone = phone
    db.commit()
    db.refresh(person)
    return person


def get_user_detail_data(db: Session, user_id: str | UUID | None) -> dict[str, Any] | None:
    """Return data needed for the user detail page."""
    user = get_subscriber(db, user_id)
    if not user:
        return None

    roles = db.execute(
        select(Role)
        .join(SystemUserRole, SystemUserRole.role_id == Role.id)
        .where(SystemUserRole.system_user_id == user.id)
        .where(Role.is_active.is_(True))
        .order_by(Role.name.asc())
    ).scalars().all()

    credential = db.execute(
        select(UserCredential)
        .where(UserCredential.system_user_id == user.id)
        .where(UserCredential.is_active.is_(True))
        .limit(1)
    ).scalars().first()

    mfa_methods = db.execute(
        select(MFAMethod).where(MFAMethod.system_user_id == user.id)
    ).scalars().all()

    return {
        "user": user,
        "roles": roles,
        "credential": credential,
        "mfa_methods": mfa_methods,
    }


def get_user_edit_data(db: Session, user_id: str | UUID | None) -> dict[str, Any] | None:
    """Return data needed for the user edit page."""
    user = get_subscriber(db, user_id)
    if not user:
        return None

    roles = db.execute(
        select(Role)
        .where(Role.is_active.is_(True))
        .order_by(Role.name.asc())
    ).scalars().all()

    current_role_ids = {
        str(role_id)
        for role_id in db.execute(
            select(SystemUserRole.role_id).where(
                SystemUserRole.system_user_id == user.id
            )
        ).scalars()
    }

    all_permissions = db.execute(
        select(Permission)
        .where(Permission.is_active.is_(True))
        .order_by(Permission.key.asc())
    ).scalars().all()

    direct_permission_ids = {
        str(permission_id)
        for permission_id in db.execute(
            select(SystemUserPermission.permission_id).where(
                SystemUserPermission.system_user_id == user.id
            )
        ).scalars()
    }

    return {
        "user": user,
        "roles": roles,
        "current_role_ids": current_role_ids,
        "all_permissions": all_permissions,
        "direct_permission_ids": direct_permission_ids,
    }


def build_profile_page_state(
    db: Session,
    *,
    current_user: dict | None,
    error: str | None = None,
    success: str | None = None,
    person_id: str | UUID | None = None,
) -> dict[str, Any]:
    resolved_person_id = person_id
    if resolved_person_id is None and current_user:
        resolved_person_id = current_user.get("person_id")
    profile_data = get_profile_data(db, resolved_person_id)
    return {
        "person": profile_data["person"],
        "credential": profile_data["credential"],
        "mfa_enabled": profile_data["mfa_enabled"],
        "api_key_count": profile_data["api_key_count"],
        "error": error,
        "success": success,
    }
