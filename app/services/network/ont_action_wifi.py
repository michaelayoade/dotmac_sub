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


def _read_param_from_cache(
    client: Any, device_id: str, full_path: str
) -> tuple[Any, str | None]:
    """Read a single parameter's (value, timestamp) from the GenieACS model cache.

    Returns (None, None) if the path is not present in the cache.
    """
    try:
        device = client.get_device(device_id)
    except Exception as exc:
        logger.debug(
            "GenieACS device cache read failed for %s path=%s: %s",
            device_id,
            full_path,
            exc,
        )
        return None, None
    node: Any = device
    for part in full_path.split("."):
        if not isinstance(node, dict):
            return None, None
        node = node.get(part)
        if node is None:
            return None, None
    if not isinstance(node, dict) or "_value" not in node:
        return None, None
    return node.get("_value"), node.get("_timestamp")


def _set_first_supported_path(
    client: Any,
    device_id: str,
    root: str,
    candidate_paths: list[str],
    value: str,
) -> dict[str, object]:
    """Try a list of candidate parameter paths until one is verified applied.

    For each candidate:
      1. Record the pre-SPV (value, timestamp) from the GenieACS cache.
      2. Issue setParameterValues.
      3. Read the post-SPV (value, timestamp) from the cache.
      4. Accept the path when the post-SPV value matches the target.
         If the timestamp did not advance, warn but still accept — the target
         value is already live, so the user's intent is satisfied.
      5. On value mismatch, try the next candidate. If all candidates end up
         with a mismatched value, raise GenieACSError so the caller reports
         failure instead of a misleading success.
    """
    last_error: Exception | None = None
    for candidate in candidate_paths:
        full_path = f"{root}.{candidate}"
        before_value, before_ts = _read_param_from_cache(client, device_id, full_path)
        params = build_tr069_params(root, {candidate: value})
        try:
            result = client.set_parameter_values(device_id, params)
        except GenieACSError as exc:
            last_error = exc
            logger.debug(
                "TR-069 path %s rejected for device %s: %s",
                full_path,
                device_id,
                exc,
            )
            continue
        after_value, after_ts = _read_param_from_cache(client, device_id, full_path)
        if after_value == value:
            if before_ts is not None and after_ts == before_ts and before_value == value:
                logger.info(
                    "TR-069 param %s on %s already at target value; no device change",
                    full_path,
                    device_id,
                )
            elif before_ts is not None and after_ts == before_ts:
                logger.warning(
                    "TR-069 param %s on %s has target value in cache but timestamp did "
                    "not advance after setParameterValues; device may not have applied",
                    full_path,
                    device_id,
                )
            _request_runtime_refresh(client, device_id, root)
            return result
        logger.warning(
            "TR-069 param %s on %s not applied after setParameterValues: expected=%r got=%r",
            full_path,
            device_id,
            value,
            after_value,
        )
        last_error = GenieACSError(
            f"Value not applied at {full_path} (expected {value!r}, cache has {after_value!r})"
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
