"""TR-069 actions for ONT remote access (SSH/Telnet) configuration."""

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
    set_and_verify,
)

logger = logging.getLogger(__name__)

# TR-069 parameter paths by data model root (vendor-specific for Huawei)
_REMOTE_ACCESS_PATHS = {
    "Device": {
        "ssh_enable": "X_HW_UserInterface.SSHEnable",
        "ssh_port": "X_HW_UserInterface.SSHPort",
        "telnet_enable": "X_HW_UserInterface.TelnetEnable",
        "telnet_port": "X_HW_UserInterface.TelnetPort",
    },
    "InternetGatewayDevice": {
        "ssh_enable": "X_HW_UserInterface.SSHEnable",
        "ssh_port": "X_HW_UserInterface.SSHPort",
        "telnet_enable": "X_HW_UserInterface.TelnetEnable",
        "telnet_port": "X_HW_UserInterface.TelnetPort",
    },
}

# Default ports
_DEFAULT_PORTS = {
    "ssh": 22,
    "telnet": 23,
}


def _get_enable_path(root: str, protocol: str) -> str:
    """Get the enable parameter path for a protocol."""
    paths = _REMOTE_ACCESS_PATHS.get(root, {})
    return paths.get(f"{protocol}_enable", "")


def _get_port_path(root: str, protocol: str) -> str:
    """Get the port parameter path for a protocol."""
    paths = _REMOTE_ACCESS_PATHS.get(root, {})
    return paths.get(f"{protocol}_port", "")


def set_wan_remote_access(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    protocol: str = "ssh",
    port: int | None = None,
) -> ActionResult:
    """Enable or disable WAN remote access via TR-069.

    Args:
        db: Database session.
        ont_id: ONT unit ID.
        enabled: True to enable, False to disable.
        protocol: "ssh" or "telnet".
        port: Optional custom port (default: 22 for SSH, 23 for Telnet).

    Returns:
        ActionResult with success/failure status.
    """
    if protocol not in ("ssh", "telnet"):
        return ActionResult(
            success=False,
            message="Protocol must be 'ssh' or 'telnet'.",
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error or resolved is None:
        return error or ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved

    # Detect data model
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    # Get parameter paths
    enable_path = _get_enable_path(root, protocol)
    port_path = _get_port_path(root, protocol)

    if not enable_path:
        return ActionResult(
            success=False,
            message=f"No {protocol.upper()} path defined for data model {root}.",
        )

    # Build parameters
    params: dict[str, str] = {
        enable_path: "1" if enabled else "0",
    }

    # Set port if enabling and port is specified or use default
    if enabled:
        effective_port = port if port is not None else _DEFAULT_PORTS.get(protocol)
        if effective_port and port_path:
            params[port_path] = str(effective_port)

    # Build full paths with root prefix
    full_params = build_tr069_params(root, params)

    # Build expected values for verification (only the enable flag)
    expected = build_tr069_params(root, {enable_path: "1" if enabled else "0"})

    # Push config
    try:
        set_and_verify(
            client,
            device_id,
            full_params,
            expected=expected,
        )
        action = "enabled" if enabled else "disabled"
        logger.info(
            "WAN %s access %s on ONT %s",
            protocol.upper(),
            action,
            ont.serial_number,
        )
        return ActionResult(
            success=True,
            message=f"WAN {protocol.upper()} access {action}.",
        )
    except GenieACSError as exc:
        logger.error(
            "Failed to set WAN remote access on ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to configure remote access: {exc}",
        )


def set_wan_remote_access_best_effort(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    protocol: str = "ssh",
    port: int | None = None,
) -> ActionResult:
    """Enable or disable WAN remote access via TR-069 with best-effort semantics.

    Similar to set_wan_remote_access but does not require verification to succeed.
    Useful when the parameter paths may not be supported by the device.

    Args:
        db: Database session.
        ont_id: ONT unit ID.
        enabled: True to enable, False to disable.
        protocol: "ssh" or "telnet".
        port: Optional custom port.

    Returns:
        ActionResult with success/failure status.
    """
    if protocol not in ("ssh", "telnet"):
        return ActionResult(
            success=False,
            message="Protocol must be 'ssh' or 'telnet'.",
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error or resolved is None:
        return error or ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved

    # Detect data model
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    # Get parameter paths
    enable_path = _get_enable_path(root, protocol)
    port_path = _get_port_path(root, protocol)

    if not enable_path:
        return ActionResult(
            success=False,
            message=f"No {protocol.upper()} path defined for data model {root}.",
        )

    # Build parameters
    params: dict[str, str] = {
        enable_path: "1" if enabled else "0",
    }

    if enabled:
        effective_port = port if port is not None else _DEFAULT_PORTS.get(protocol)
        if effective_port and port_path:
            params[port_path] = str(effective_port)

    full_params = build_tr069_params(root, params)

    # Try to push config, don't require verification
    try:
        result: dict[str, Any] = client.set_parameter_values(device_id, full_params)
        action = "enabled" if enabled else "disabled"
        logger.info(
            "WAN %s access %s on ONT %s (best-effort)",
            protocol.upper(),
            action,
            ont.serial_number,
        )
        return ActionResult(
            success=True,
            message=f"WAN {protocol.upper()} access {action} (config pushed).",
            data=result,
        )
    except GenieACSError as exc:
        logger.warning(
            "Failed to set WAN remote access on ONT %s (best-effort): %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to configure remote access: {exc}",
        )
