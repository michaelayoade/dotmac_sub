"""Service helpers for admin system role form pages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rbac import Permission, RolePermission
from app.schemas.rbac import RoleCreate, RolePermissionCreate, RoleUpdate
from app.services import rbac as rbac_service


def get_permissions_for_form(db: Session):
    """Return permission options for role forms."""
    return rbac_service.permissions.list(
        db=db,
        is_active=None,
        order_by="key",
        order_dir="asc",
        limit=1000,
        offset=0,
    )


def build_role_create_payload(
    *,
    name: str,
    description: str | None,
    is_active: bool,
) -> RoleCreate:
    """Build normalized RoleCreate payload."""
    description_value = description.strip() if description else None
    return RoleCreate(
        name=name.strip(),
        description=description_value or None,
        is_active=is_active,
    )


def build_role_update_payload(
    *,
    name: str,
    description: str | None,
    is_active: bool,
) -> RoleUpdate:
    description_value = description.strip() if description else None
    return RoleUpdate(
        name=name.strip(),
        description=description_value or None,
        is_active=is_active,
    )


def create_role_with_permissions(
    db: Session,
    *,
    payload: RoleCreate,
    permission_ids: list[str],
):
    """Create role and attach selected permissions."""
    role = rbac_service.roles.create(db, payload)
    normalized_ids = normalize_permission_ids(permission_ids)
    for permission_id in normalized_ids:
        rbac_service.role_permissions.create(
            db,
            RolePermissionCreate(
                role_id=role.id,
                permission_id=UUID(permission_id),
            ),
        )
    return role


def get_role_edit_data(db: Session, role_id: str):
    """Return role form data for editing."""
    role = rbac_service.roles.get(db, role_id)
    permissions = get_permissions_for_form(db)
    selected_permission_ids = {
        str(permission_id)
        for permission_id in db.execute(
            select(RolePermission.permission_id).where(RolePermission.role_id == role.id)
        ).scalars()
    }
    return {
        "role": role,
        "permissions": permissions,
        "selected_permission_ids": selected_permission_ids,
    }


def normalize_permission_ids(permission_ids: list[str]) -> set[str]:
    """Normalize posted permission ids into validated UUID-string set."""
    normalized: set[str] = set()
    for permission_id in permission_ids:
        try:
            normalized.add(str(UUID(permission_id)))
        except (TypeError, ValueError):
            continue
    return normalized


def sync_role_permissions(db: Session, *, role_id, permission_ids: list[str]) -> None:
    """Replace role-permission links with desired set."""
    desired_ids = {UUID(permission_id) for permission_id in normalize_permission_ids(permission_ids)}
    if desired_ids:
        found_ids = {
            str(permission_id)
            for permission_id in db.execute(
                select(Permission.id).where(Permission.id.in_(desired_ids))
            ).scalars()
        }
        missing = {str(permission_id) for permission_id in desired_ids} - found_ids
        if missing:
            raise ValueError("One or more permissions were not found.")

    existing_links = db.execute(
        select(RolePermission).where(RolePermission.role_id == role_id)
    ).scalars().all()
    existing_ids = {link.permission_id: link for link in existing_links}

    for permission_id, link in existing_ids.items():
        if permission_id not in desired_ids:
            db.delete(link)
    for permission_id in desired_ids - set(existing_ids.keys()):
        db.add(RolePermission(role_id=role_id, permission_id=permission_id))


def update_role_with_permissions(
    db: Session,
    *,
    role_id: str,
    payload: RoleUpdate,
    permission_ids: list[str],
) -> None:
    """Update role attributes and sync permissions in a single transaction."""
    role = rbac_service.roles.update(db, role_id, payload)
    sync_role_permissions(db, role_id=role.id, permission_ids=permission_ids)
    db.commit()


def build_role_error_state(
    db: Session,
    *,
    role: dict,
    permission_ids: list[str],
) -> dict[str, object]:
    return {
        "role": role,
        "permissions": get_permissions_for_form(db),
        "selected_permission_ids": normalize_permission_ids(permission_ids),
    }
