"""WiFi and LAN-related ONT actions."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    build_tr069_params,
    detect_data_model_root,
    get_ont_or_error,
    resolve_client_or_error,
)

logger = logging.getLogger(__name__)

# TR-069 parameter suffixes by data model root
_WIFI_SSID_PATHS = {
    "Device": "WiFi.SSID.1.SSID",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.SSID",
}

_WIFI_PSK_PATHS = {
    "Device": "WiFi.AccessPoint.1.Security.PreSharedKey.1.PreSharedKey",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
}

_LAN_PORT_PATHS = {
    "Device": "Ethernet.Interface.{port}.Enable",
    "InternetGatewayDevice": "LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable",
}


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    """Set WiFi SSID on ONT via TR-069."""
    if not ssid or len(ssid) > 32:
        return ActionResult(success=False, message="SSID must be 1-32 characters.")

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    params = build_tr069_params(root, {_WIFI_SSID_PATHS[root]: ssid})
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info("WiFi SSID set on ONT %s to '%s'", ont.serial_number, ssid)
        return ActionResult(
            success=True,
            message=f"WiFi SSID updated to '{ssid}' on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Set WiFi SSID failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to set SSID: {exc}")


def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
    """Set WiFi password on ONT via TR-069."""
    if not password or len(password) < 8:
        return ActionResult(success=False, message="WiFi password must be at least 8 characters.")

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    params = build_tr069_params(root, {_WIFI_PSK_PATHS[root]: password})
    try:
        result = client.set_parameter_values(device_id, params)
        logger.info("WiFi password set on ONT %s", ont.serial_number)
        return ActionResult(
            success=True,
            message=f"WiFi password updated on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Set WiFi password failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to set WiFi password: {exc}")


def toggle_lan_port(db: Session, ont_id: str, port: int, enabled: bool) -> ActionResult:
    """Enable or disable an ONT LAN port via TR-069."""
    if port < 1 or port > 4:
        return ActionResult(success=False, message="Port number must be between 1 and 4.")

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    value = "true" if enabled else "false"
    path = _LAN_PORT_PATHS[root].format(port=port)
    params = build_tr069_params(root, {path: value})
    try:
        result = client.set_parameter_values(device_id, params)
        action_word = "enabled" if enabled else "disabled"
        logger.info("LAN port %d %s on ONT %s", port, action_word, ont.serial_number)
        return ActionResult(
            success=True,
            message=f"LAN port {port} {action_word} on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Toggle LAN port %d failed for ONT %s: %s", port, ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to toggle LAN port: {exc}")
