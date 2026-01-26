from fastapi import Depends

from app.db import get_db
from app.services.auth_dependencies import (
    require_audit_auth,
    require_permission,
    require_role,
    require_user_auth,
)


def get_current_user(auth=Depends(require_user_auth)):
    """Get current authenticated user info.

    Returns a dict with person_id, session_id, roles, and scopes.
    """
    return auth


__all__ = [
    "get_db",
    "get_current_user",
    "require_audit_auth",
    "require_permission",
    "require_role",
    "require_user_auth",
]
