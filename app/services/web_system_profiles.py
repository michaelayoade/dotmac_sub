"""Service helpers for admin system user profile/detail/edit pages."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.rbac import Role, SubscriberRole as SubscriberRoleModel
from app.models.subscriber import Subscriber
from app.services import rbac as rbac_service
from app.services.common import coerce_uuid


def get_subscriber(db: Session, user_id: str | UUID | None) -> Subscriber | None:
    """Resolve a subscriber by id."""
    if not user_id:
        return None
    return db.get(Subscriber, coerce_uuid(user_id))


def get_profile_data(db: Session, person_id: str | UUID | None) -> dict[str, Any]:
    """Return profile page data for the logged-in user."""
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
        .where(UserCredential.subscriber_id == person.id)
        .where(UserCredential.is_active.is_(True))
        .limit(1)
    ).scalars().first()

    mfa_enabled = bool(
        db.scalar(
            select(MFAMethod.id)
            .where(MFAMethod.subscriber_id == person.id)
            .where(MFAMethod.enabled.is_(True))
            .limit(1)
        )
    )

    api_key_count = (
        db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.subscriber_id == person.id)
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
    person: Subscriber,
    first_name: str | None,
    last_name: str | None,
    email: str | None,
    phone: str | None,
) -> Subscriber:
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
        .join(SubscriberRoleModel, SubscriberRoleModel.role_id == Role.id)
        .where(SubscriberRoleModel.subscriber_id == user.id)
        .where(Role.is_active.is_(True))
        .order_by(Role.name.asc())
    ).scalars().all()

    credential = db.execute(
        select(UserCredential)
        .where(UserCredential.subscriber_id == user.id)
        .where(UserCredential.is_active.is_(True))
        .limit(1)
    ).scalars().first()

    mfa_methods = db.execute(
        select(MFAMethod).where(MFAMethod.subscriber_id == user.id)
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

    roles = rbac_service.roles.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    current_role_ids = {
        str(role_id)
        for role_id in db.execute(
            select(SubscriberRoleModel.role_id).where(
                SubscriberRoleModel.subscriber_id == user.id
            )
        ).scalars()
    }

    all_permissions = rbac_service.permissions.list(
        db=db,
        is_active=True,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    direct_permissions = rbac_service.subscriber_permissions.list_for_subscriber(
        db, str(user.id)
    )
    direct_permission_ids = {str(permission.permission_id) for permission in direct_permissions}

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
