"""View-context helpers for admin system role/permission forms."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services import rbac as rbac_service
from app.services import web_system_role_forms as web_system_role_forms_service


def get_role_new_form_context(db: Session) -> dict:
    """Return template context fragment for role create form."""
    return {
        "role": None,
        "permissions": web_system_role_forms_service.get_permissions_for_form(db),
        "selected_permission_ids": set(),
        "action_url": "/admin/system/roles",
        "form_title": "New Role",
        "submit_label": "Create Role",
    }


def get_permission_new_form_context() -> dict:
    """Return template context fragment for permission create form."""
    return {
        "permission": None,
        "action_url": "/admin/system/permissions",
        "form_title": "New Permission",
        "submit_label": "Create Permission",
    }


def get_permission_edit_form_context(db: Session, permission_id: str) -> dict | None:
    """Return template context fragment for permission edit form, or None if missing."""
    try:
        permission = rbac_service.permissions.get(db, permission_id)
    except Exception:
        return None
    return {
        "permission": permission,
        "action_url": f"/admin/system/permissions/{permission_id}",
        "form_title": "Edit Permission",
        "submit_label": "Save Changes",
    }
