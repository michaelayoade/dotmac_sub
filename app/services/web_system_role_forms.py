"""Service helpers for admin system role form pages."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.rbac import Permission, RolePermission
from app.schemas.rbac import RoleCreate, RoleUpdate
from app.services import rbac_catalog
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)


def get_permissions_for_form(db: Session):
    """Return permission options for role forms."""
    return (
        db.execute(
            select(Permission)
            .where(Permission.is_active.is_(True))
            .where(Permission.is_ui_assignable.is_(True))
            .order_by(Permission.key.asc())
        )
        .scalars()
        .all()
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
    context: CommandContext,
):
    """Create a role and its complete permission policy atomically."""
    return rbac_catalog.create_role(
        db,
        rbac_catalog.CreateRoleCommand(
            context=context,
            name=payload.name,
            description=payload.description,
            is_active=payload.is_active,
            permission_ids=parse_permission_ids(permission_ids),
        ),
    )


def get_role_edit_data(db: Session, role_id: str):
    """Return role form data for editing."""
    role = rbac_catalog.get_role(db, UUID(role_id))
    if role is None:
        raise ValueError("Role not found")
    permissions = get_permissions_for_form(db)
    selected_permission_ids = {
        str(permission_id)
        for permission_id in db.execute(
            select(RolePermission.permission_id).where(
                RolePermission.role_id == role.id
            )
        ).scalars()
    }
    return {
        "role": role,
        "permissions": permissions,
        "selected_permission_ids": selected_permission_ids,
    }


def parse_permission_ids(permission_ids: list[str]) -> tuple[UUID, ...]:
    """Reject malformed permission identifiers instead of silently dropping them."""

    return tuple(dict.fromkeys(UUID(permission_id) for permission_id in permission_ids))


def normalize_permission_ids(permission_ids: list[str]) -> set[str]:
    """Normalize identifiers for error-form presentation only."""

    normalized: set[str] = set()
    for permission_id in permission_ids:
        try:
            normalized.add(str(UUID(permission_id)))
        except (TypeError, ValueError):
            continue
    return normalized


def update_role_with_permissions(
    db: Session,
    *,
    role_id: str,
    payload: RoleUpdate,
    permission_ids: list[str],
    context: CommandContext,
) -> None:
    """Update role attributes and its complete permission policy atomically."""
    rbac_catalog.update_role(
        db,
        rbac_catalog.UpdateRoleCommand(
            context=context,
            role_id=UUID(role_id),
            name=payload.name,
            description=payload.description,
            update_description=True,
            is_active=payload.is_active,
            permission_ids=parse_permission_ids(permission_ids),
        ),
    )


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
