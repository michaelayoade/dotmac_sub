"""WiFi and LAN-related ONT actions."""

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
)

logger = logging.getLogger(__name__)

# TR-069 parameter suffixes by data model root
_WIFI_SSID_PATHS = {
    "Device": "WiFi.SSID.1.SSID",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.SSID",
}

_WIFI_ENABLE_PATHS = {
    "Device": "WiFi.SSID.1.Enable",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.Enable",
}

_WIFI_CHANNEL_PATHS = {
    "Device": "WiFi.Radio.1.Channel",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.Channel",
}

_WIFI_SECURITY_PATHS = {
    "Device": "WiFi.AccessPoint.1.Security.ModeEnabled",
    "InternetGatewayDevice": "LANDevice.1.WLANConfiguration.1.BeaconType",
}

_WIFI_PSK_PATHS = {
    "Device": [
        "WiFi.AccessPoint.1.Security.KeyPassphrase",
        "WiFi.AccessPoint.1.Security.PreSharedKey.1.PreSharedKey",
    ],
    "InternetGatewayDevice": [
        "LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
        "LANDevice.1.WLANConfiguration.1.KeyPassphrase",
        "LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
    ],
}

_LAN_PORT_PATHS = {
    "Device": "Ethernet.Interface.{port}.Enable",
    "InternetGatewayDevice": "LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable",
}


def _request_runtime_refresh(client: Any, device_id: str, root: str) -> None:
    """Best-effort refresh so UI snapshots catch up after a config push."""
    refresh = getattr(client, "refresh_object", None)
    if not callable(refresh):
        return
    try:
        refresh(device_id, f"{root}.", connection_request=True)
    except Exception:
        logger.debug(
            "Runtime refresh request failed for device %s after WiFi update",
            device_id,
            exc_info=True,
        )


def _set_first_supported_path(
    client: Any,
    device_id: str,
    root: str,
    candidate_paths: list[str],
    value: str,
) -> dict[str, object]:
    """Try a list of candidate parameter paths until one succeeds."""
    last_error: Exception | None = None
    for candidate in candidate_paths:
        params = build_tr069_params(root, {candidate: value})
        try:
            result = client.set_parameter_values(device_id, params)
            _request_runtime_refresh(client, device_id, root)
            return result
        except GenieACSError as exc:
            last_error = exc
            logger.debug(
                "TR-069 path %s rejected for device %s: %s",
                f"{root}.{candidate}",
                device_id,
                exc,
            )
    if last_error is not None:
        raise last_error
    raise GenieACSError("No WiFi parameter paths configured.")


def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
    """Set WiFi SSID on ONT via TR-069."""
    if not ssid or len(ssid) > 32:
        return ActionResult(success=False, message="SSID must be 1-32 characters.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    params = build_tr069_params(root, {_WIFI_SSID_PATHS[root]: ssid})
    try:
        result = client.set_parameter_values(device_id, params)
        _request_runtime_refresh(client, device_id, root)
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
        return ActionResult(
            success=False, message="WiFi password must be at least 8 characters."
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    try:
        result = _set_first_supported_path(
            client,
            device_id,
            root,
            _WIFI_PSK_PATHS[root],
            password,
        )
        logger.info("WiFi password set on ONT %s", ont.serial_number)
        return ActionResult(
            success=True,
            message=f"WiFi password updated on {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Set WiFi password failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(
            success=False, message=f"Failed to set WiFi password: {exc}"
        )


def set_wifi_config(
    db: Session,
    ont_id: str,
    *,
    enabled: bool | None = None,
    ssid: str | None = None,
    password: str | None = None,
    channel: int | None = None,
    security_mode: str | None = None,
) -> ActionResult:
    """Set common WiFi radio/SSID/security fields via TR-069."""
    if ssid is not None and (not ssid or len(ssid) > 32):
        return ActionResult(success=False, message="SSID must be 1-32 characters.")
    if password is not None and len(password) < 8:
        return ActionResult(
            success=False, message="WiFi password must be at least 8 characters."
        )
    if channel is not None and not 0 <= channel <= 196:
        return ActionResult(success=False, message="WiFi channel is out of range.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)

    params: dict[str, str] = {}
    changed: list[str] = []
    if enabled is not None:
        params[_WIFI_ENABLE_PATHS[root]] = "true" if enabled else "false"
        changed.append("enabled" if enabled else "disabled")
    if ssid is not None:
        params[_WIFI_SSID_PATHS[root]] = ssid
        changed.append(f"SSID {ssid}")
    if channel is not None:
        params[_WIFI_CHANNEL_PATHS[root]] = str(channel)
        changed.append(f"channel {channel}")
    if security_mode:
        params[_WIFI_SECURITY_PATHS[root]] = security_mode
        changed.append(f"security {security_mode}")

    if not params and password is None:
        return ActionResult(
            success=False, message="At least one WiFi setting is required."
        )

    try:
        result: dict[str, object] = {}
        if params:
            result = client.set_parameter_values(
                device_id, build_tr069_params(root, params)
            )
            _request_runtime_refresh(client, device_id, root)
        if password is not None:
            result = _set_first_supported_path(
                client,
                device_id,
                root,
                _WIFI_PSK_PATHS[root],
                password,
            )
            changed.append("password")
        logger.info("WiFi config set on ONT %s: %s", ont.serial_number, changed)
        return ActionResult(
            success=True,
            message=f"WiFi config updated on {ont.serial_number}: {', '.join(changed)}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Set WiFi config failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to set WiFi config: {exc}")


def toggle_lan_port(db: Session, ont_id: str, port: int, enabled: bool) -> ActionResult:
    """Enable or disable an ONT LAN port via TR-069."""
    if port < 1 or port > 4:
        return ActionResult(
            success=False, message="Port number must be between 1 and 4."
        )

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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
        logger.error(
            "Toggle LAN port %d failed for ONT %s: %s", port, ont.serial_number, exc
        )
        return ActionResult(success=False, message=f"Failed to toggle LAN port: {exc}")
