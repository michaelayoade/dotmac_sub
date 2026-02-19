"""Service helpers for admin system permission form pages."""

from __future__ import annotations

from app.schemas.rbac import PermissionCreate, PermissionUpdate


def build_permission_create_payload(
    *,
    key: str,
    description: str | None,
    is_active: bool,
) -> PermissionCreate:
    """Build normalized PermissionCreate payload."""
    description_value = description.strip() if description else None
    return PermissionCreate(
        key=key.strip(),
        description=description_value or None,
        is_active=is_active,
    )


def build_permission_update_payload(
    *,
    key: str,
    description: str | None,
    is_active: bool,
) -> PermissionUpdate:
    """Build normalized PermissionUpdate payload."""
    description_value = description.strip() if description else None
    return PermissionUpdate(
        key=key.strip(),
        description=description_value or None,
        is_active=is_active,
    )


def build_permission_error_state(
    *,
    permission: dict,
    action_url: str,
    form_title: str,
    submit_label: str,
) -> dict[str, object]:
    return {
        "permission": permission,
        "action_url": action_url,
        "form_title": form_title,
        "submit_label": submit_label,
    }
