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
    set_and_verify,
)

logger = logging.getLogger(__name__)

# TR-069 parameter suffixes by data model root
_WIFI_SSID_CANDIDATE_PATHS = {
    "Device": [f"WiFi.SSID.{idx}.SSID" for idx in range(1, 9)],
    "InternetGatewayDevice": [
        f"LANDevice.1.WLANConfiguration.{idx}.SSID" for idx in range(1, 9)
    ],
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

# Security-mode value normalization by data-model root. The UI/callers use
# TR-181-style names ("WPA2-Personal", "WPA-WPA2-Personal", ...) but TR-098's
# BeaconType only accepts None / Basic / WPA / 11i / WPAand11i. Map both
# directions through a single lower-cased alias table so the same input
# (from the UI or from another code path) always lands in the device-native
# value — which is also what the post-SPV readback will see.
_SECURITY_MODE_ALIASES: dict[str, dict[str, str]] = {
    "InternetGatewayDevice": {
        # No security
        "none": "None",
        "open": "None",
        # WEP (legacy)
        "wep": "Basic",
        "basic": "Basic",
        # WPA
        "wpa": "WPA",
        "wpa-personal": "WPA",
        "wpapsk": "WPA",
        "wpa-psk": "WPA",
        # WPA2 (all common variants)
        "wpa2": "11i",
        "wpa2-personal": "11i",
        "wpa2psk": "11i",
        "wpa2-psk": "11i",
        "11i": "11i",
        # WPA + WPA2 mixed
        "wpa-wpa2": "WPAand11i",
        "wpa-wpa2-personal": "WPAand11i",
        "wpa/wpa2": "WPAand11i",
        "wpa2/wpa": "WPAand11i",
        "wpaand11i": "WPAand11i",
        "mixed": "WPAand11i",
        # WPA3 (maps to best available - WPA2 on TR-098 devices)
        "wpa3": "11i",
        "wpa3-personal": "11i",
        "wpa3psk": "11i",
        "wpa3-psk": "11i",
        "wpa3-sae": "11i",
        "sae": "11i",
    },
    "Device": {
        # No security
        "none": "None",
        "open": "None",
        # WEP (legacy)
        "wep": "WEP-128",
        "wep-64": "WEP-64",
        "wep-128": "WEP-128",
        # WPA
        "wpa": "WPA-Personal",
        "wpa-personal": "WPA-Personal",
        "wpapsk": "WPA-Personal",
        "wpa-psk": "WPA-Personal",
        # WPA2 (all common variants)
        "wpa2": "WPA2-Personal",
        "wpa2-personal": "WPA2-Personal",
        "wpa2psk": "WPA2-Personal",
        "wpa2-psk": "WPA2-Personal",
        # WPA + WPA2 mixed
        "wpa-wpa2": "WPA-WPA2-Personal",
        "wpa-wpa2-personal": "WPA-WPA2-Personal",
        "wpa/wpa2": "WPA-WPA2-Personal",
        "wpa2/wpa": "WPA-WPA2-Personal",
        "mixed": "WPA-WPA2-Personal",
        # WPA3
        "wpa3": "WPA3-Personal",
        "wpa3-personal": "WPA3-Personal",
        "wpa3psk": "WPA3-Personal",
        "wpa3-psk": "WPA3-Personal",
        "wpa3-sae": "WPA3-Personal",
        "sae": "WPA3-Personal",
        # WPA2 + WPA3 mixed
        "wpa2-wpa3": "WPA2-WPA3-Personal",
        "wpa2-wpa3-personal": "WPA2-WPA3-Personal",
        "wpa2/wpa3": "WPA2-WPA3-Personal",
        "wpa3/wpa2": "WPA2-WPA3-Personal",
    },
}


def _normalize_security_mode(mode: str, root: str) -> str:
    """Return the data-model-native security-mode string for ``mode``.

    Falls back to the original input (trimmed) when the alias is unknown —
    callers can pass an exact device-native value and have it land verbatim.
    """
    key = (mode or "").strip().lower()
    mapping = _SECURITY_MODE_ALIASES.get(root, {})
    return mapping.get(key, (mode or "").strip())


_WIFI_PSK_PATHS = {
    "Device": [
        "WiFi.AccessPoint.1.Security.KeyPassphrase",
        "WiFi.AccessPoint.1.Security.PreSharedKey.1.PreSharedKey",
    ],
    # For InternetGatewayDevice (TR-098), try PreSharedKey.1.PreSharedKey first
    # as Huawei devices reject KeyPassphrase with fault 9007 "Invalid parameter value"
    "InternetGatewayDevice": [
        "LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
        "LANDevice.1.WLANConfiguration.1.KeyPassphrase",
        "LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
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
    *,
    allow_empty_readback: bool = False,
) -> dict[str, object]:
    """Try a list of candidate parameter paths until one is verified applied.

    For each candidate, call ``set_and_verify`` which chains a live read in
    the same CWMP session. On GenieACSError (the path is unsupported or the
    live read shows the device did not apply the value), try the next
    candidate. If none verify, re-raise the last error so the caller surfaces
    an actionable failure instead of a misleading success.

    When ``allow_empty_readback`` is True (for password fields), if verification
    fails because readback returned empty (devices mask passwords), fall back to
    an unverified write and consider it successful.
    """
    last_error: Exception | None = None
    for candidate in candidate_paths:
        full_path = f"{root}.{candidate}"
        params = build_tr069_params(root, {candidate: value})
        try:
            result = set_and_verify(client, device_id, params)
        except GenieACSError as exc:
            error_str = str(exc)
            # Check if the error is due to empty readback (password masking)
            if allow_empty_readback and "got=''" in error_str:
                # Password fields often return empty on readback for security.
                # Try an unverified write instead.
                logger.info(
                    "TR-069 path %s returned empty on readback (password masking); "
                    "attempting unverified write on %s",
                    full_path,
                    device_id,
                )
                try:
                    result = client.set_parameter_values(
                        device_id, params, connection_request=True
                    )
                    _request_runtime_refresh(client, device_id, root)
                    return result
                except GenieACSError as inner_exc:
                    last_error = inner_exc
                    logger.debug(
                        "Unverified write to %s failed on %s: %s",
                        full_path,
                        device_id,
                        inner_exc,
                    )
                    continue
            last_error = exc
            logger.debug(
                "TR-069 path %s rejected or not applied on device %s: %s",
                full_path,
                device_id,
                exc,
            )
            continue
        _request_runtime_refresh(client, device_id, root)
        return result
    if last_error is not None:
        raise last_error
    raise GenieACSError("No WiFi parameter paths configured.")


def _set_single_wifi_field(
    client: Any,
    device_id: str,
    root: str,
    path: str,
    value: str,
    *,
    verify: bool,
) -> dict[str, object]:
    params = build_tr069_params(root, {path: value})
    if verify:
        return set_and_verify(client, device_id, params)
    return client.set_parameter_values(device_id, params, connection_request=True)


def _set_best_effort_wifi_field(
    client: Any,
    device_id: str,
    root: str,
    path: str,
    value: str,
    label: str,
    *,
    allow_unverified: bool,
) -> dict[str, object] | None:
    """Write a WiFi field that is commonly write-only or omitted on readback."""
    try:
        return _set_single_wifi_field(
            client,
            device_id,
            root,
            path,
            value,
            verify=True,
        )
    except GenieACSError as exc:
        if not allow_unverified:
            raise
        logger.info(
            "WiFi %s on %s did not verify via readback; sending best-effort SPV: %s",
            label,
            device_id,
            exc,
        )
        try:
            result = _set_single_wifi_field(
                client,
                device_id,
                root,
                path,
                value,
                verify=False,
            )
        except GenieACSError:
            logger.info(
                "WiFi %s best-effort SPV failed on %s",
                label,
                device_id,
                exc_info=True,
            )
            return None
        _request_runtime_refresh(client, device_id, root)
        return result


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
    try:
        result = _set_first_supported_path(
            client,
            device_id,
            root,
            _WIFI_SSID_CANDIDATE_PATHS[root],
            ssid,
        )
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
            allow_empty_readback=True,  # Password fields often return empty on readback
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

    changed: list[str] = []
    if enabled is not None:
        changed.append("enabled" if enabled else "disabled")
    if ssid is not None:
        changed.append(f"SSID {ssid}")
    if channel is not None:
        changed.append(f"channel {channel}")
    if security_mode:
        normalized_mode = _normalize_security_mode(security_mode, root)
        changed.append(f"security {normalized_mode}")

    if not changed and password is None:
        return ActionResult(
            success=False, message="At least one WiFi setting is required."
        )

    try:
        result: dict[str, object] = {}
        allow_unverified_optional = ssid is not None or password is not None
        if ssid is not None:
            result = _set_first_supported_path(
                client,
                device_id,
                root,
                _WIFI_SSID_CANDIDATE_PATHS[root],
                ssid,
            )
        if enabled is not None:
            best_effort = _set_best_effort_wifi_field(
                client,
                device_id,
                root,
                _WIFI_ENABLE_PATHS[root],
                "true" if enabled else "false",
                "enable",
                allow_unverified=allow_unverified_optional,
            )
            if best_effort is not None:
                result = best_effort
        if channel is not None:
            best_effort = _set_best_effort_wifi_field(
                client,
                device_id,
                root,
                _WIFI_CHANNEL_PATHS[root],
                str(channel),
                "channel",
                allow_unverified=allow_unverified_optional,
            )
            if best_effort is not None:
                result = best_effort
        if security_mode:
            normalized_mode = _normalize_security_mode(security_mode, root)
            best_effort = _set_best_effort_wifi_field(
                client,
                device_id,
                root,
                _WIFI_SECURITY_PATHS[root],
                normalized_mode,
                "security",
                allow_unverified=allow_unverified_optional,
            )
            if best_effort is not None:
                result = best_effort
        if password is not None:
            result = _set_first_supported_path(
                client,
                device_id,
                root,
                _WIFI_PSK_PATHS[root],
                password,
                allow_empty_readback=True,  # Password fields often return empty on readback
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
        result = set_and_verify(client, device_id, params)
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
