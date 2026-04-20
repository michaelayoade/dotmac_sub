"""MikroTik vendor-specific helpers for NAS device management."""

import logging
import re
import secrets
import string
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import (
    NasDevice,
    NasVendor,
    ProvisioningAction,
    ProvisioningLog,
    ProvisioningLogStatus,
)
from app.schemas.catalog import NasDeviceUpdate
from app.services.credential_crypto import decrypt_credential
from app.services.db_session_adapter import db_session_adapter
from app.services.nas._helpers import (
    merge_single_tag,
    prefixed_value_from_tags,
)

logger = logging.getLogger(__name__)


def _as_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_routeros_uptime_to_seconds(value: object) -> int | None:
    """Parse RouterOS uptime strings into seconds."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    match = re.fullmatch(
        r"(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})",
        text,
    )
    if match:
        weeks = int(match.group("w") or 0)
        days = int(match.group("d") or 0)
        hours = int(match.group("h") or 0)
        minutes = int(match.group("m") or 0)
        seconds = int(match.group("s") or 0)
        return (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds

    # Older RouterOS API commonly reports uptime in token form (e.g. 1w2d3h4m5s).
    token_match = re.fullmatch(
        r"(?:(?P<w>\d+)w)?(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?",
        text,
    )
    if not token_match:
        return None
    if not any(token_match.group(name) for name in ("w", "d", "h", "m", "s")):
        return None
    weeks = int(token_match.group("w") or 0)
    days = int(token_match.group("d") or 0)
    hours = int(token_match.group("h") or 0)
    minutes = int(token_match.group("m") or 0)
    seconds = int(token_match.group("s") or 0)
    return (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds


def _mikrotik_api_port(device: NasDevice) -> int:
    raw_port = prefixed_value_from_tags(device.tags, "mikrotik_api_port:")
    if raw_port:
        try:
            return int(raw_port)
        except (TypeError, ValueError):
            pass
    return 8728


def _mikrotik_rest_auth(
    device: NasDevice,
) -> tuple[str, tuple[str, str] | None, dict[str, str], bool]:
    """Build MikroTik REST auth context."""

    if device.vendor != NasVendor.mikrotik:
        raise HTTPException(
            status_code=400,
            detail="Vendor-specific API status is only available for MikroTik devices.",
        )
    if not device.api_url:
        raise HTTPException(status_code=400, detail="API URL is not configured.")

    auth = None
    headers: dict[str, str] = {}
    if device.api_token:
        headers["Authorization"] = f"Bearer {decrypt_credential(device.api_token)}"
    elif device.api_username and device.api_password:
        username = cast(str, device.api_username)
        password = cast(str, decrypt_credential(device.api_password))
        auth = (username, password)
    else:
        raise HTTPException(
            status_code=400, detail="API credentials are not configured."
        )

    base_url = device.api_url.rstrip("/")
    verify_tls = device.api_verify_tls if device.api_verify_tls is not None else False
    return base_url, auth, headers, verify_tls


def _mikrotik_routeros_auth(device: NasDevice) -> tuple[str, int, str, str]:
    if device.vendor != NasVendor.mikrotik:
        raise HTTPException(
            status_code=400,
            detail="Vendor-specific API status is only available for MikroTik devices.",
        )

    host = device.management_ip or device.ip_address
    if not host:
        raise HTTPException(status_code=400, detail="Management IP is not configured.")
    if not (device.api_username and device.api_password):
        raise HTTPException(
            status_code=400, detail="API credentials are not configured."
        )
    username = cast(str, device.api_username)
    password = cast(str, decrypt_credential(device.api_password))
    return host, _mikrotik_api_port(device), username, password


def _mikrotik_rest_get(
    *,
    base_url: str,
    path: str,
    auth: tuple[str, str] | None,
    headers: dict[str, str],
    verify_tls: bool,
    timeout: int = 10,
) -> object:
    """Issue a GET request to MikroTik REST endpoint."""
    import requests

    resp = requests.get(
        f"{base_url}{path}",
        auth=auth,
        headers=headers,
        timeout=timeout,
        verify=verify_tls,
    )
    resp.raise_for_status()
    if not resp.text:
        return {}
    return resp.json()


def _select_primary_mac(interfaces: object) -> str | None:
    """Select the most useful MAC address from interface list."""
    if not isinstance(interfaces, list):
        return None

    candidates: list[tuple[int, str]] = []
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        mac = item.get("mac-address") or item.get("mac_address")
        if not mac:
            continue
        name = str(item.get("name") or "").strip().lower()
        default_name = (
            str(item.get("default-name") or item.get("default_name") or "")
            .strip()
            .lower()
        )
        disabled = str(item.get("disabled") or "").lower() == "true"
        running = str(item.get("running") or "").lower() == "true"

        rank = 100
        iface_key = default_name or name
        if iface_key in {"ether1", "sfp1"}:
            rank = 10
        elif iface_key.startswith("ether"):
            rank = 20
        elif iface_key.startswith("sfp"):
            rank = 30
        elif "bridge" in iface_key:
            rank = 40
        if running and not disabled:
            rank -= 5
        candidates.append((rank, str(mac)))

    if not candidates:
        return None
    candidates.sort(key=lambda row: row[0])
    return candidates[0][1]


def _mikrotik_status_from_rest(device: NasDevice) -> dict[str, object]:
    base_url, auth, headers, verify_tls = _mikrotik_rest_auth(device)
    resource_data = _mikrotik_rest_get(
        base_url=base_url,
        path="/rest/system/resource",
        auth=auth,
        headers=headers,
        verify_tls=verify_tls,
    )
    package_data = _mikrotik_rest_get(
        base_url=base_url,
        path="/rest/system/package",
        auth=auth,
        headers=headers,
        verify_tls=verify_tls,
    )
    try:
        routerboard_data = _mikrotik_rest_get(
            base_url=base_url,
            path="/rest/system/routerboard",
            auth=auth,
            headers=headers,
            verify_tls=verify_tls,
        )
    except Exception:
        routerboard_data = {}
    try:
        interfaces_raw = _mikrotik_rest_get(
            base_url=base_url,
            path="/rest/interface",
            auth=auth,
            headers=headers,
            verify_tls=verify_tls,
        )
    except Exception:
        interfaces_raw = []

    version = None
    if isinstance(package_data, list):
        for item in package_data:
            if isinstance(item, dict) and str(item.get("name")).lower() == "routeros":
                version = item.get("version")
                break
    if version is None and isinstance(resource_data, dict):
        version = resource_data.get("version")
    uptime_raw = (
        resource_data.get("uptime") if isinstance(resource_data, dict) else None
    )
    uptime_seconds = _parse_routeros_uptime_to_seconds(uptime_raw)
    serial_number = None
    if isinstance(routerboard_data, dict):
        serial_number = routerboard_data.get("serial-number") or routerboard_data.get(
            "serial_number"
        )
    primary_mac = _select_primary_mac(interfaces_raw)

    return {
        "platform": resource_data.get("platform")
        if isinstance(resource_data, dict)
        else None,
        "board_name": resource_data.get("board-name")
        if isinstance(resource_data, dict)
        else None,
        "routeros_version": version,
        "serial_number": serial_number,
        "primary_mac": primary_mac,
        "architecture_name": resource_data.get("architecture-name")
        if isinstance(resource_data, dict)
        else None,
        "cpu_model": resource_data.get("cpu")
        if isinstance(resource_data, dict)
        else None,
        "cpu_count": resource_data.get("cpu-count")
        if isinstance(resource_data, dict)
        else None,
        "cpu_frequency": resource_data.get("cpu-frequency")
        if isinstance(resource_data, dict)
        else None,
        "total_hdd_space": resource_data.get("total-hdd-space")
        if isinstance(resource_data, dict)
        else None,
        "free_hdd_space": resource_data.get("free-hdd-space")
        if isinstance(resource_data, dict)
        else None,
        "cpu_usage": resource_data.get("cpu-load")
        if isinstance(resource_data, dict)
        else None,
        "total_memory": resource_data.get("total-memory")
        if isinstance(resource_data, dict)
        else None,
        "free_memory": resource_data.get("free-memory")
        if isinstance(resource_data, dict)
        else None,
        "uptime": uptime_raw,
        "uptime_seconds": uptime_seconds,
        "ipv6_status": (
            "enabled"
            if isinstance(resource_data, dict) and resource_data.get("ipv6")
            else "unknown"
        ),
        "last_status_check": datetime.now(UTC),
        "api_source": "rest",
    }


def _mikrotik_routeros_query(
    device: NasDevice,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    from routeros_api import RouterOsApiPool

    host, port, username, password = _mikrotik_routeros_auth(device)
    use_ssl = port == 8729
    pool = RouterOsApiPool(
        host,
        username=username,
        password=password,
        port=port,
        plaintext_login=not use_ssl,
        use_ssl=use_ssl,
        ssl_verify=False,
        ssl_verify_hostname=False,
    )
    try:
        api = pool.get_api()
        raw_resource = cast(Any, api.get_resource("/system/resource")).get()
        resource = (
            raw_resource[0] if isinstance(raw_resource, list) and raw_resource else {}
        )
        raw_interfaces = cast(Any, api.get_resource("/interface")).get()
        interfaces = _as_dict_list(raw_interfaces)
        try:
            raw_ppp_active = cast(Any, api.get_resource("/ppp/active")).get()
            ppp_active = _as_dict_list(raw_ppp_active)
        except Exception:
            ppp_active = []
        return cast(dict[str, object], resource), interfaces, ppp_active
    finally:
        pool.disconnect()


def _mikrotik_status_from_routeros_api(device: NasDevice) -> dict[str, object]:
    resource_data, interfaces, _ppp_active = _mikrotik_routeros_query(device)
    uptime_raw = resource_data.get("uptime")
    uptime_seconds = _parse_routeros_uptime_to_seconds(uptime_raw)
    serial_number = resource_data.get("serial-number") or resource_data.get(
        "serial_number"
    )
    if not serial_number:
        try:
            from routeros_api import RouterOsApiPool

            host, port, username, password = _mikrotik_routeros_auth(device)
            use_ssl = port == 8729
            pool = RouterOsApiPool(
                host,
                username=username,
                password=password,
                port=port,
                plaintext_login=not use_ssl,
                use_ssl=use_ssl,
                ssl_verify=False,
                ssl_verify_hostname=False,
            )
            try:
                api = pool.get_api()
                rb = cast(Any, api.get_resource("/system/routerboard")).get()
                if isinstance(rb, list) and rb and isinstance(rb[0], dict):
                    serial_number = rb[0].get("serial-number") or rb[0].get(
                        "serial_number"
                    )
            finally:
                pool.disconnect()
        except Exception:
            serial_number = None
    primary_mac = _select_primary_mac(interfaces)
    return {
        "platform": resource_data.get("platform"),
        "board_name": resource_data.get("board-name"),
        "routeros_version": resource_data.get("version"),
        "serial_number": serial_number,
        "primary_mac": primary_mac,
        "architecture_name": resource_data.get("architecture-name"),
        "cpu_model": resource_data.get("cpu"),
        "cpu_count": resource_data.get("cpu-count"),
        "cpu_frequency": resource_data.get("cpu-frequency"),
        "total_hdd_space": resource_data.get("total-hdd-space"),
        "free_hdd_space": resource_data.get("free-hdd-space"),
        "cpu_usage": resource_data.get("cpu-load"),
        "total_memory": resource_data.get("total-memory"),
        "free_memory": resource_data.get("free-memory"),
        "uptime": uptime_raw,
        "uptime_seconds": uptime_seconds,
        "ipv6_status": "unknown",
        "last_status_check": datetime.now(UTC),
        "api_source": "routeros_api",
    }


def _record_mikrotik_auth_attempt(
    *,
    nas_device_id: UUID,
    method: str,
    success: bool,
    execution_time_ms: int | None,
    error: str | None = None,
) -> None:
    """Persist MikroTik auth/connect attempt as provisioning log."""
    session = db_session_adapter.create_session()
    try:
        log = ProvisioningLog(
            nas_device_id=nas_device_id,
            action=ProvisioningAction.get_user_info,
            command_sent=f"mikrotik_auth:{method}",
            response_received="connected" if success else None,
            status=ProvisioningLogStatus.success
            if success
            else ProvisioningLogStatus.failed,
            error_message=error,
            execution_time_ms=execution_time_ms,
            triggered_by="system",
            request_data={"kind": "mikrotik_auth_attempt", "method": method},
        )
        session.add(log)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def get_mikrotik_api_status(
    device: NasDevice, *, db: Session | None = None
) -> dict[str, object]:
    """Test MikroTik API and return basic runtime status fields."""
    rest_error: Exception | None = None
    started = time.perf_counter()
    try:
        status = _mikrotik_status_from_rest(device)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if db is not None:
            _record_mikrotik_auth_attempt(
                nas_device_id=device.id,
                method="rest",
                success=True,
                execution_time_ms=elapsed_ms,
            )
        return status
    except Exception as exc:
        rest_error = exc
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if db is not None:
            _record_mikrotik_auth_attempt(
                nas_device_id=device.id,
                method="rest",
                success=False,
                execution_time_ms=elapsed_ms,
                error=str(exc),
            )

    started = time.perf_counter()
    try:
        status = _mikrotik_status_from_routeros_api(device)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if db is not None:
            _record_mikrotik_auth_attempt(
                nas_device_id=device.id,
                method="routeros_api",
                success=True,
                execution_time_ms=elapsed_ms,
            )
        return status
    except Exception as api_exc:
        rest_msg = str(rest_error) if rest_error else "not attempted"
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if db is not None:
            _record_mikrotik_auth_attempt(
                nas_device_id=device.id,
                method="routeros_api",
                success=False,
                execution_time_ms=elapsed_ms,
                error=str(api_exc),
            )
        raise HTTPException(
            status_code=400,
            detail=f"MikroTik API test failed. REST error: {rest_msg}. RouterOS API error: {api_exc}",
        ) from api_exc


def get_mikrotik_api_telemetry(
    device: NasDevice, *, db: Session | None = None
) -> dict[str, object]:
    """Fetch MikroTik telemetry with REST-first, RouterOS-API fallback."""
    status = get_mikrotik_api_status(device, db=db)
    source = str(status.get("api_source") or "")

    def _to_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _to_int(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    if source != "rest":
        try:
            _resource, routeros_interfaces, routeros_ppp_active = (
                _mikrotik_routeros_query(device)
            )
        except Exception:
            interfaces: list[dict[str, object]] = []
            ppp_active: list[dict[str, object]] = []
        else:
            interfaces = routeros_interfaces
            ppp_active = routeros_ppp_active
    else:
        base_url, auth, headers, verify_tls = _mikrotik_rest_auth(device)
        interfaces_raw: object = []
        ppp_active_raw: object = []
        try:
            interfaces_raw = _mikrotik_rest_get(
                base_url=base_url,
                path="/rest/interface",
                auth=auth,
                headers=headers,
                verify_tls=verify_tls,
            )
        except Exception:
            interfaces_raw = []
        try:
            ppp_active_raw = _mikrotik_rest_get(
                base_url=base_url,
                path="/rest/ppp/active",
                auth=auth,
                headers=headers,
                verify_tls=verify_tls,
            )
        except Exception:
            ppp_active_raw = []
        interfaces = _as_dict_list(interfaces_raw)
        ppp_active = _as_dict_list(ppp_active_raw)

    rx_bps = 0.0
    tx_bps = 0.0
    interface_up = 0
    interface_down = 0
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        running = str(item.get("running") or "").lower() == "true"
        disabled = str(item.get("disabled") or "").lower() == "true"
        if running and not disabled:
            interface_up += 1
        else:
            interface_down += 1

        rx_val = (
            _to_float(item.get("rx-bits-per-second"))
            or _to_float(item.get("rx-bps"))
            or 0.0
        )
        tx_val = (
            _to_float(item.get("tx-bits-per-second"))
            or _to_float(item.get("tx-bps"))
            or 0.0
        )
        rx_bps += rx_val
        tx_bps += tx_val

    total_memory = _to_int(status.get("total_memory") or status.get("total-memory"))
    free_memory = _to_int(status.get("free_memory") or status.get("free-memory"))
    memory_percent: float | None = None
    if total_memory and total_memory > 0 and free_memory is not None:
        used = max(total_memory - free_memory, 0)
        memory_percent = (used / total_memory) * 100.0

    pppoe_active = 0
    for session in ppp_active:
        if not isinstance(session, dict):
            continue
        service = str(session.get("service") or "").lower()
        if "pppoe" in service:
            pppoe_active += 1

    status["rx_bps"] = rx_bps
    status["tx_bps"] = tx_bps
    status["active_subscribers"] = len(ppp_active)
    status["pppoe_active_subscribers"] = pppoe_active
    status["interface_up"] = interface_up
    status["interface_down"] = interface_down
    status["memory_percent"] = memory_percent
    return status


def refresh_mikrotik_status_for_device(db: Session, *, device_id: str) -> str:
    from app.services.nas.devices import NasDevices

    device = NasDevices.get(db, device_id)
    status = get_mikrotik_api_status(device, db=db)
    tags = device.tags
    tags = merge_single_tag(
        tags, "mikrotik_status_platform:", str(status.get("platform") or "-")
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_board_name:", str(status.get("board_name") or "-")
    )
    tags = merge_single_tag(
        tags,
        "mikrotik_status_routeros_version:",
        str(status.get("routeros_version") or "-"),
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_serial_number:", str(status.get("serial_number") or "-")
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_primary_mac:", str(status.get("primary_mac") or "-")
    )
    tags = merge_single_tag(
        tags,
        "mikrotik_status_architecture_name:",
        str(status.get("architecture_name") or "-"),
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_cpu_model:", str(status.get("cpu_model") or "-")
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_cpu_count:", str(status.get("cpu_count") or "-")
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_cpu_frequency:", str(status.get("cpu_frequency") or "-")
    )
    tags = merge_single_tag(
        tags,
        "mikrotik_status_total_hdd_space:",
        str(status.get("total_hdd_space") or "-"),
    )
    tags = merge_single_tag(
        tags,
        "mikrotik_status_free_hdd_space:",
        str(status.get("free_hdd_space") or "-"),
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_cpu_usage:", str(status.get("cpu_usage") or "-")
    )
    tags = merge_single_tag(
        tags, "mikrotik_status_ipv6_status:", str(status.get("ipv6_status") or "-")
    )
    last_check = status.get("last_status_check")
    tags = merge_single_tag(
        tags, "mikrotik_status_last_check:", str(last_check) if last_check else "-"
    )
    NasDevices.update(
        db,
        device_id,
        NasDeviceUpdate(
            model=str(status.get("board_name") or "").strip() or None,
            firmware_version=str(status.get("routeros_version") or "").strip() or None,
            serial_number=str(status.get("serial_number") or "").strip() or None,
            tags=tags,
        ),
    )
    return (
        f"Connected. Platform={status.get('platform') or '-'}, "
        f"Board={status.get('board_name') or '-'}, "
        f"RouterOS={status.get('routeros_version') or '-'}"
    )


def _generate_router_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "@#%+=_-"
    return "".join(secrets.choice(alphabet) for _ in range(max(12, length)))


def generate_mikrotik_bootstrap_script_for_device(
    db: Session,
    *,
    device_id: str,
    username: str = "dotmacapi",
    api_port: int = 8728,
    rest_port: int = 443,
) -> dict[str, str]:
    """Rotate MikroTik API credentials and return RouterOS terminal bootstrap script."""
    from app.services.nas.devices import NasDevices

    device = NasDevices.get(db, device_id)
    host = (device.management_ip or device.ip_address or "").strip()
    if not host:
        raise HTTPException(
            status_code=400,
            detail="Management IP is not configured for this NAS device.",
        )

    username = (username or "dotmacapi").strip().lower() or "dotmacapi"
    api_port = max(1, min(int(api_port or 8728), 65535))
    rest_port = max(1, min(int(rest_port or 443), 65535))
    password = _generate_router_password(20)

    tags = device.tags
    tags = merge_single_tag(tags, "mikrotik_api_enabled:", "true")
    tags = merge_single_tag(tags, "mikrotik_api_port:", str(api_port))

    NasDevices.update(
        db,
        device_id,
        NasDeviceUpdate(
            vendor=NasVendor.mikrotik,
            api_url=f"https://{host}",
            api_username=username,
            api_password=password,
            api_verify_tls=False,
            tags=tags,
        ),
    )

    script_lines = [
        "# Dotmac MikroTik API bootstrap",
        f"# Device: {device.name} ({host})",
        f"# Generated: {datetime.now(UTC).isoformat()}",
        "",
        f':local existing [/user find where name="{username}"]',
        ":if ([:len $existing] = 0) do={",
        f'    /user add name="{username}" password="{password}" group=read comment="dotmac-api"',
        "} else={",
        f'    /user set $existing password="{password}" group=read',
        "}",
        "",
        f"/ip service set [find name=api] disabled=no port={api_port}",
        ':if ([:len [/ip service find where name="www-ssl"]] > 0) do={',
        f"    /ip service set [find name=www-ssl] disabled=no port={rest_port}",
        "}",
        "",
        f':put "DOTMAC_API_USERNAME={username}"',
        f':put "DOTMAC_API_PASSWORD={password}"',
    ]
    return {
        "script": "\n".join(script_lines),
        "username": username,
        "password": password,
        "api_url": f"https://{host}",
        "api_port": str(api_port),
        "rest_port": str(rest_port),
    }
