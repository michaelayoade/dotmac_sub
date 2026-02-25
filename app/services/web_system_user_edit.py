"""Mutation helper for admin system user edit form submission."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.auth import AuthProvider, UserCredential
from app.models.rbac import Permission, Role, SystemUserPermission, SystemUserRole
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services import web_system_profiles as web_system_profiles_service
from app.services.auth_flow import hash_password
from app.services.common import coerce_uuid


def get_subscriber_or_none(db: Session, user_id: str) -> SystemUser | None:
    return db.get(SystemUser, coerce_uuid(user_id))


def parse_edit_form(form_data) -> dict[str, object]:
    return {
        "first_name": form_data.get("first_name", ""),
        "last_name": form_data.get("last_name", ""),
        "display_name": form_data.get("display_name"),
        "email": form_data.get("email", ""),
        "phone": form_data.get("phone"),
        "user_type": form_data.get("user_type"),
        "is_active": form_data.get("is_active"),
        "new_password": form_data.get("new_password"),
        "confirm_password": form_data.get("confirm_password"),
        "require_password_change": form_data.get("require_password_change"),
        "role_ids": form_data.getlist("role_ids"),
        "direct_permission_ids": form_data.getlist("direct_permission_ids"),
    }


def build_edit_state(db: Session, *, subscriber: SystemUser) -> dict[str, object]:
    edit_data = web_system_profiles_service.get_user_edit_data(db, str(subscriber.id))
    if edit_data is not None:
        return edit_data
    return {
        "user": subscriber,
        "roles": db.execute(
            select(Role).where(Role.is_active.is_(True)).order_by(Role.name.asc())
        ).scalars().all(),
        "current_role_ids": set(),
        "all_permissions": db.execute(
            select(Permission)
            .where(Permission.is_active.is_(True))
            .order_by(Permission.key.asc())
        ).scalars().all(),
        "direct_permission_ids": set(),
    }


def apply_user_edit(
    db: Session,
    *,
    subscriber: SystemUser,
    first_name: str,
    last_name: str,
    display_name: str | None,
    email: str,
    phone: str | None,
    user_type: str | None,
    is_active: bool,
    role_ids: list[str],
    direct_permission_ids: list[str],
    new_password: str | None,
    confirm_password: str | None,
    require_password_change: bool,
    is_admin: bool,
    actor_id: str | None,
) -> None:
    """Apply submitted user edit changes and commit."""
    subscriber.first_name = first_name.strip()
    subscriber.last_name = last_name.strip()
    subscriber.display_name = display_name.strip() if display_name else None
    subscriber.email = email.strip()
    subscriber.phone = phone.strip() if phone else None
    subscriber.user_type = UserType.system_user
    subscriber.is_active = is_active

    db.query(UserCredential).filter(
        UserCredential.system_user_id == subscriber.id,
        UserCredential.provider == AuthProvider.local,
        UserCredential.is_active.is_(True),
    ).update({"username": email.strip()})

    desired_role_ids = set(role_ids)
    existing_roles = db.query(SystemUserRole).filter(
        SystemUserRole.system_user_id == subscriber.id
    ).all()
    existing_role_map = {str(link.role_id): link for link in existing_roles}

    for role_id_str, role_link in existing_role_map.items():
        if role_id_str not in desired_role_ids:
            db.delete(role_link)

    for role_id_str in desired_role_ids:
        if role_id_str not in existing_role_map:
            db.add(
                SystemUserRole(
                    system_user_id=subscriber.id,
                    role_id=UUID(role_id_str),
                )
            )

    db.query(SystemUserPermission).filter(
        SystemUserPermission.system_user_id == subscriber.id
    ).delete(synchronize_session=False)

    granted_by = coerce_uuid(actor_id) if actor_id else None
    for permission_id in set(direct_permission_ids):
        db.add(
            SystemUserPermission(
                system_user_id=subscriber.id,
                permission_id=UUID(permission_id),
                granted_by_system_user_id=granted_by,
            )
        )

    if new_password or confirm_password:
        if not is_admin:
            raise ValueError("Only admins can update passwords.")
        if not new_password or not confirm_password:
            raise ValueError("Password and confirmation are required.")
        if new_password != confirm_password:
            raise ValueError("Passwords do not match.")

        updated = db.query(UserCredential).filter(
            UserCredential.system_user_id == subscriber.id,
            UserCredential.provider == AuthProvider.local,
            UserCredential.is_active.is_(True),
        ).update(
            {
                "password_hash": hash_password(new_password),
                "must_change_password": require_password_change,
                "password_updated_at": datetime.now(UTC),
            }
        )
        if not updated:
            db.add(
                UserCredential(
                    system_user_id=subscriber.id,
                    provider=AuthProvider.local,
                    username=email.strip(),
                    password_hash=hash_password(new_password),
                    must_change_password=require_password_change,
                )
            )

    db.commit()
