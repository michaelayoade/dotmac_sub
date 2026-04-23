"""TR-069 actions for ONT web management credentials."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_ont_client_or_error,
    persist_data_model_root,
)

logger = logging.getLogger(__name__)

# TR-069 parameter paths by data model root
_WEB_CREDENTIAL_PATHS = {
    "Device": {
        "username": "Users.User.1.Username",
        "password": "Users.User.1.Password",
    },
    "InternetGatewayDevice": {
        # Huawei vendor-specific path
        "username": "X_HW_WebUserInfo.1.UserName",
        "password": "X_HW_WebUserInfo.1.Password",
    },
}


def set_web_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
) -> ActionResult:
    """Set ONT web management credentials via TR-069.

    Args:
        db: Database session.
        ont_id: ONT unit ID.
        username: New web admin username.
        password: New web admin password.

    Returns:
        ActionResult with success/failure status.
    """
    if not username or not password:
        return ActionResult(
            success=False,
            message="Both username and password are required.",
        )

    if len(password) < 6:
        return ActionResult(
            success=False,
            message="Password must be at least 6 characters.",
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error or resolved is None:
        return error or ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved

    # Detect data model
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    # Get parameter paths
    paths = _WEB_CREDENTIAL_PATHS.get(root, {})
    username_path = paths.get("username")
    password_path = paths.get("password")

    if not username_path or not password_path:
        return ActionResult(
            success=False,
            message=f"No web credential paths defined for data model {root}.",
        )

    # Build parameters
    params: dict[str, str] = {
        username_path: username,
        password_path: password,
    }

    full_params = build_tr069_params(root, params)

    # Push config (best-effort, don't verify password readback)
    try:
        result: dict[str, Any] = client.set_parameter_values(device_id, full_params)
        logger.info(
            "Web credentials updated on ONT %s (user: %s)",
            ont.serial_number,
            username,
        )
        return ActionResult(
            success=True,
            message=f"Web credentials updated on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error(
            "Failed to set web credentials on ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to update web credentials: {exc}",
        )
