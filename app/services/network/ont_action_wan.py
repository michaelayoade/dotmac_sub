"""WAN configuration actions for ONTs via TR-069.

This module handles WAN-related TR-069 actions including:
- PPPoE credential configuration
- WAN mode switching (PPPoE, DHCP, Static, Bridge)
- WAN instance creation via addObject
- IPv6 configuration
- HTTP management toggle

Factory-fresh ONTs lack WANPPPConnection instances. setParameterValues silently
no-ops when the target object doesn't exist. This module calls addObject first
to create instances before configuring them.

Usage::

    from app.services.network.ont_action_wan import set_pppoe_credentials

    result = set_pppoe_credentials(
        db, ont_id,
        username="100014919",
        password="secret123",
        instance_index=1,
        ensure_instance=True,
        wan_vlan=100,
    )
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    detect_data_model_root,
    get_ont_client_or_error,
    persist_data_model_root,
    set_and_verify,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Time to wait before verifying addObject result
_WAN_ADD_OBJECT_VERIFY_DELAY_SECONDS = 60
# How long to consider a pending addObject operation valid
_WAN_ADD_OBJECT_PENDING_TTL_SECONDS = 10 * 60

# WAN connection type mappings for TR-098
_IGD_CONNECTION_TYPE_BY_MODE = {
    "pppoe": "IP_Routed",
    "dhcp": "IP_Routed",
    "static": "IP_Routed",
    "bridge": "IP_Bridged",
}

# Object container paths for addObject operations
_WAN_OBJECT_CONTAINERS = {
    "InternetGatewayDevice": {
        "ppp": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.",
        "ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.",
    },
    "Device": {
        "ppp": "PPP.Interface.",
        "ip": "IP.Interface.",
        "vlan": "Ethernet.VLANTermination.",
    },
}

# TR-098 WAN parameter paths
_IGD_WAN_PATHS = {
    "ppp.username": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Username",
    "ppp.password": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Password",
    "ppp.enable": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Enable",
    "ppp.nat_enabled": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.NATEnabled",
    "ppp.connection_type": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.ConnectionType",
    "ppp.vlan": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.X_HW_VLAN",
    "ppp.service_list": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.X_HW_SERVICELIST",
    "ppp.name": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Name",
    "ppp.num_entries": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnectionNumberOfEntries",
    "ip.enable": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.Enable",
    "ip.address": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ExternalIPAddress",
    "ip.subnet": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.SubnetMask",
    "ip.gateway": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DefaultGateway",
    "ip.dns": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DNSServers",
    "ip.nat_enabled": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.NATEnabled",
    "ip.connection_type": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ConnectionType",
    "ip.addressing_type": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.AddressingType",
    "ip.num_entries": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnectionNumberOfEntries",
}

# TR-181 WAN parameter paths
_TR181_WAN_PATHS = {
    "ppp.username": "PPP.Interface.{i}.Username",
    "ppp.password": "PPP.Interface.{i}.Password",
    "ppp.enable": "PPP.Interface.{i}.Enable",
    "ppp.connection_trigger": "PPP.Interface.{i}.ConnectionTrigger",
    "ppp.lower_layers": "PPP.Interface.{i}.LowerLayers",
    "ip.enable": "IP.Interface.{i}.Enable",
    "ip.type": "IP.Interface.{i}.Type",
    "ip.lower_layers": "IP.Interface.{i}.LowerLayers",
    "ip.ipv4_enable": "IP.Interface.{i}.IPv4Enable",
    "ip.ipv6_enable": "IP.Interface.{i}.IPv6Enable",
    "ip.static_address": "IP.Interface.{i}.IPv4Address.1.IPAddress",
    "ip.static_subnet": "IP.Interface.{i}.IPv4Address.1.SubnetMask",
    "vlan.id": "Ethernet.VLANTermination.{i}.VLANID",
    "vlan.enable": "Ethernet.VLANTermination.{i}.Enable",
    "dhcp.enable": "DHCPv4.Client.{i}.Enable",
    "dhcpv6.enable": "DHCPv6.Client.{i}.Enable",
    "dhcpv6.request_prefixes": "DHCPv6.Client.{i}.RequestPrefixes",
}

# HTTP/Web management paths (vendor-specific, typically Huawei)
_HTTP_MGMT_PATHS = {
    "Device": {
        "enable": "X_HW_UserInterface.WebUIEnable",
        "port": "X_HW_UserInterface.WebUIPort",
    },
    "InternetGatewayDevice": {
        "enable": "X_HW_UserInterface.WebUIEnable",
        "port": "X_HW_UserInterface.WebUIPort",
    },
}


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    """Parse ISO 8601 timestamp string."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int_value(value: Any) -> int:
    """Convert value to int, defaulting to 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _runtime_capabilities(ont: Any) -> dict[str, Any]:
    """Get runtime capabilities from ONT snapshot."""
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}
    return capabilities


def _persist_runtime_capabilities(ont: Any, capabilities: dict[str, Any]) -> None:
    """Persist runtime capabilities to ONT snapshot."""
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        snapshot = {}
    else:
        snapshot = dict(snapshot)
    snapshot["capabilities"] = capabilities
    ont.tr069_last_snapshot = snapshot
    flag_modified(ont, "tr069_last_snapshot")
    ont.tr069_last_snapshot_at = datetime.now(UTC)


def _resolve_wan_path(
    root: str,
    path_key: str,
    instance_index: int = 1,
) -> str:
    """Resolve a WAN parameter path for the given data model root."""
    if root == "Device":
        path_map = _TR181_WAN_PATHS
    else:
        path_map = _IGD_WAN_PATHS

    template = path_map.get(path_key)
    if not template:
        raise ValueError(f"Unknown WAN path key: {path_key}")

    suffix = template.replace("{i}", str(instance_index))
    return f"{root}.{suffix}"


def _get_wan_object_container(
    root: str,
    wan_type: str,
    instance_index: int = 1,
) -> str:
    """Get the object container path for addObject."""
    containers = _WAN_OBJECT_CONTAINERS.get(root, {})
    template = containers.get(wan_type)
    if not template:
        raise ValueError(f"Unknown WAN type: {wan_type} for root: {root}")
    return f"{root}.{template.replace('{i}', str(instance_index))}"


def _pending_wan_add_object(
    ont: Any,
    instance_index: int,
    wan_type: str,
    wan_vlan: int | None,
) -> bool:
    """Check if a WAN addObject operation is pending verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if not isinstance(pending, dict):
        return False

    pending_key = f"add_{wan_type}_wan"
    add_action = pending.get(pending_key)
    if not isinstance(add_action, dict):
        return False

    if add_action.get("state") != "pending_verification":
        return False
    if int(add_action.get("instance_index") or 0) != instance_index:
        return False
    if str(add_action.get("wan_vlan") or "") != str(wan_vlan or ""):
        return False

    requested_at = _parse_iso(add_action.get("requested_at"))
    if requested_at is None:
        return False

    return datetime.now(UTC) - requested_at < timedelta(
        seconds=_WAN_ADD_OBJECT_PENDING_TTL_SECONDS
    )


def _mark_wan_add_object_pending(
    ont: Any,
    *,
    wan_type: str,
    root: str,
    instance_index: int,
    wan_vlan: int | None,
    object_path: str,
) -> None:
    """Mark a WAN addObject operation as pending verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.setdefault("pending_actions", {})
    pending_key = f"add_{wan_type}_wan"
    pending[pending_key] = {
        "state": "pending_verification",
        "requested_at": _iso_now(),
        "root": root,
        "instance_index": instance_index,
        "wan_vlan": wan_vlan,
        "object_path": object_path,
    }
    wan = capabilities.setdefault("wan", {})
    wan[f"supports_tr069_add_{wan_type}_wan"] = "pending_verification"
    _persist_runtime_capabilities(ont, capabilities)


def _clear_wan_add_object_pending(
    ont: Any,
    wan_type: str,
    *,
    success: bool = True,
) -> None:
    """Clear pending WAN addObject state after verification."""
    capabilities = _runtime_capabilities(ont)
    pending = capabilities.get("pending_actions")
    if isinstance(pending, dict):
        pending.pop(f"add_{wan_type}_wan", None)
        if not pending:
            capabilities.pop("pending_actions", None)

    wan = capabilities.setdefault("wan", {})
    if success:
        wan[f"has_{wan_type}_wan"] = True
        wan[f"supports_tr069_add_{wan_type}_wan"] = True
        wan[f"supports_tr069_set_{wan_type}_credentials"] = True
        wan[f"requires_precreated_{wan_type}_wan"] = False
    else:
        wan[f"supports_tr069_add_{wan_type}_wan"] = False
        wan[f"requires_precreated_{wan_type}_wan"] = True

    _persist_runtime_capabilities(ont, capabilities)


def _igd_wan_details(
    client: Any,
    device: dict[str, Any],
    root: str,
    instance_index: int,
) -> dict[str, Any]:
    """Extract WAN details from an IGD device."""
    ppp_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANPPPConnection.1"
    )
    ip_base = (
        f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}.WANIPConnection.1"
    )

    def _val(path: str) -> Any:
        return client.extract_parameter_value(device, path)

    return {
        "ppp_name": _val(f"{ppp_base}.Name"),
        "ppp_status": _val(f"{ppp_base}.ConnectionStatus"),
        "ppp_ip": _val(f"{ppp_base}.ExternalIPAddress"),
        "ppp_username": _val(f"{ppp_base}.Username"),
        "ppp_service": _val(f"{ppp_base}.X_HW_SERVICELIST"),
        "ppp_vlan": _val(f"{ppp_base}.X_HW_VLAN"),
        "ip_name": _val(f"{ip_base}.Name"),
        "ip_status": _val(f"{ip_base}.ConnectionStatus"),
        "ip_address": _val(f"{ip_base}.ExternalIPAddress"),
        "ip_service": _val(f"{ip_base}.X_HW_SERVICELIST"),
        "ip_vlan": _val(f"{ip_base}.X_HW_VLAN"),
    }


def _igd_ppp_container_conflict(
    details: dict[str, Any],
    wan_vlan: int | None,
) -> str | None:
    """Check if the selected WAN container conflicts with the requested config."""
    service = str(details.get("ppp_service") or details.get("ip_service") or "").upper()
    detected_vlan = str(details.get("ppp_vlan") or details.get("ip_vlan") or "").strip()
    requested_vlan = str(wan_vlan or "").strip()

    if service == "TR069":
        return "the selected WANConnectionDevice is the TR-069 management WAN"
    if requested_vlan and detected_vlan and detected_vlan != requested_vlan:
        return (
            f"the selected WANConnectionDevice is VLAN {detected_vlan}, "
            f"not requested PPPoE VLAN {requested_vlan}"
        )
    return None


# ---------------------------------------------------------------------------
# WAN Instance Management
# ---------------------------------------------------------------------------


def probe_wan_instance(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_type: str = "ppp",
) -> ActionResult:
    """Probe whether a WAN instance exists on the ONT.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        instance_index: WAN instance index (default 1).
        wan_type: Type of WAN interface ("ppp" or "ip").

    Returns:
        ActionResult with data containing:
        - exists: bool - Whether the instance exists
        - instance_index: int - The probed instance index
        - wan_type: str - The probed WAN type
        - details: dict - Additional WAN details (for IGD)
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    exists = False
    details: dict[str, Any] = {}

    if root == "InternetGatewayDevice":
        if wan_type == "ppp":
            entries_path = _resolve_wan_path(root, "ppp.num_entries", instance_index)
        else:
            entries_path = _resolve_wan_path(root, "ip.num_entries", instance_index)

        count = _int_value(client.extract_parameter_value(device, entries_path))
        exists = count >= 1
        details = _igd_wan_details(client, device, root, instance_index)
    else:
        # TR-181: Check if the interface exists
        if wan_type == "ppp":
            enable_path = _resolve_wan_path(root, "ppp.enable", instance_index)
        else:
            enable_path = _resolve_wan_path(root, "ip.enable", instance_index)

        enable_value = client.extract_parameter_value(device, enable_path)
        exists = enable_value is not None

    return ActionResult(
        success=True,
        message=f"WAN {wan_type} instance {instance_index} {'exists' if exists else 'does not exist'}.",
        data={
            "exists": exists,
            "instance_index": instance_index,
            "wan_type": wan_type,
            "root": root,
            "details": details,
        },
    )


def ensure_wan_instance(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_type: str = "ppp",
    wan_vlan: int | None = None,
) -> ActionResult:
    """Ensure a WAN instance exists, creating it via addObject if necessary.

    This function handles the critical addObject flow for factory-fresh ONTs
    that lack WANPPPConnection instances.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        instance_index: WAN instance index (default 1).
        wan_type: Type of WAN interface ("ppp" or "ip").
        wan_vlan: Optional VLAN to verify against existing config.

    Returns:
        ActionResult with:
        - success=True if instance exists or was created
        - waiting=True if addObject was issued but needs verification
        - data containing instance details
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    # Check current instance count
    if root == "InternetGatewayDevice":
        if wan_type == "ppp":
            entries_path = _resolve_wan_path(root, "ppp.num_entries", instance_index)
        else:
            entries_path = _resolve_wan_path(root, "ip.num_entries", instance_index)

        count = _int_value(client.extract_parameter_value(device, entries_path))

        if count >= 1:
            # Instance exists - verify no conflict
            details = _igd_wan_details(client, device, root, instance_index)
            conflict = _igd_ppp_container_conflict(details, wan_vlan)
            if conflict:
                return ActionResult(
                    success=False,
                    message=f"Cannot use WAN instance {instance_index}: {conflict}",
                    data={
                        "conflict": True,
                        "instance_index": instance_index,
                        "details": details,
                    },
                )

            _clear_wan_add_object_pending(ont, wan_type, success=True)
            db.flush()
            return ActionResult(
                success=True,
                message=f"WAN {wan_type} instance {instance_index} exists and is ready.",
                data={
                    "exists": True,
                    "created": False,
                    "instance_index": instance_index,
                    "details": details,
                },
            )

        # Check if addObject is pending
        if _pending_wan_add_object(ont, instance_index, wan_type, wan_vlan):
            # Trigger a refresh to check if object was created
            refresh = getattr(client, "refresh_object", None)
            if callable(refresh):
                try:
                    object_path = _get_wan_object_container(
                        root, wan_type, instance_index
                    )
                    refresh(device_id, object_path)
                except GenieACSError:
                    logger.debug(
                        "WAN addObject pending refresh failed for %s",
                        device_id,
                        exc_info=True,
                    )

            return ActionResult(
                success=False,
                waiting=True,
                message=(
                    f"WAN {wan_type} addObject is pending. "
                    "Retry after device completes the operation."
                ),
                data={
                    "pending": True,
                    "instance_index": instance_index,
                    "wan_vlan": wan_vlan,
                    "retry_after_seconds": _WAN_ADD_OBJECT_VERIFY_DELAY_SECONDS,
                },
            )

        # Need to create the instance
        object_path = _get_wan_object_container(root, wan_type, instance_index)
        try:
            client.add_object(device_id, object_path)
            _mark_wan_add_object_pending(
                ont,
                wan_type=wan_type,
                root=root,
                instance_index=instance_index,
                wan_vlan=wan_vlan,
                object_path=object_path,
            )

            # Trigger refresh to verify creation
            refresh = getattr(client, "refresh_object", None)
            if callable(refresh):
                refresh(device_id, object_path)

            # Re-check the count
            refreshed = client.get_device(device_id)
            refreshed_count = _int_value(
                client.extract_parameter_value(refreshed, entries_path)
            )

            if refreshed_count >= 1:
                _clear_wan_add_object_pending(ont, wan_type, success=True)
                db.flush()
                return ActionResult(
                    success=True,
                    message=f"WAN {wan_type} instance {instance_index} created successfully.",
                    data={
                        "exists": True,
                        "created": True,
                        "instance_index": instance_index,
                    },
                )

            # addObject issued but not yet visible
            db.flush()
            return ActionResult(
                success=False,
                waiting=True,
                message=(
                    f"WAN {wan_type} addObject issued. Instance not visible yet. "
                    "Retry after device processes the operation."
                ),
                data={
                    "pending": True,
                    "add_object_issued": True,
                    "instance_index": instance_index,
                    "wan_vlan": wan_vlan,
                    "retry_after_seconds": _WAN_ADD_OBJECT_VERIFY_DELAY_SECONDS,
                },
            )

        except GenieACSError as exc:
            _clear_wan_add_object_pending(ont, wan_type, success=False)
            db.flush()
            return ActionResult(
                success=False,
                message=f"Failed to create WAN {wan_type} instance: {exc}",
                data={
                    "add_object_rejected": True,
                    "error": str(exc),
                },
            )

    else:
        # TR-181: Check if interfaces exist
        if wan_type == "ppp":
            enable_path = _resolve_wan_path(root, "ppp.enable", instance_index)
        else:
            enable_path = _resolve_wan_path(root, "ip.enable", instance_index)

        enable_value = client.extract_parameter_value(device, enable_path)

        if enable_value is not None:
            return ActionResult(
                success=True,
                message=f"TR-181 WAN {wan_type} instance {instance_index} exists.",
                data={
                    "exists": True,
                    "created": False,
                    "instance_index": instance_index,
                },
            )

        # TR-181 typically requires OMCI pre-provisioning
        return ActionResult(
            success=False,
            message=(
                f"No TR-181 {wan_type.upper()} interface exists at index {instance_index}. "
                "TR-181 devices typically require OMCI pre-provisioning to create "
                "the WAN stack before TR-069 can configure credentials."
            ),
            data={
                "tr181_stack_incomplete": True,
                "requires_omci": True,
                "instance_index": instance_index,
            },
        )


# ---------------------------------------------------------------------------
# PPPoE Configuration
# ---------------------------------------------------------------------------


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    instance_index: int = 1,
    ensure_instance: bool = True,
    wan_vlan: int | None = None,
) -> ActionResult:
    """Set PPPoE credentials on an ONT via TR-069.

    This is the primary entry point for PPPoE credential provisioning.
    It handles the addObject flow for factory-fresh ONTs.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        username: PPPoE username.
        password: PPPoE password.
        instance_index: WAN instance index (default 1).
        ensure_instance: If True, create PPP instance if missing (default True).
        wan_vlan: Optional VLAN to verify against existing config.

    Returns:
        ActionResult indicating success/failure.
    """
    if not username:
        return ActionResult(success=False, message="PPPoE username is required.")
    if not password:
        return ActionResult(success=False, message="PPPoE password is required.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    # Ensure PPP instance exists if requested
    if ensure_instance:
        ensure_result = ensure_wan_instance(
            db,
            ont_id,
            instance_index=instance_index,
            wan_type="ppp",
            wan_vlan=wan_vlan,
        )
        if not ensure_result.success:
            return ensure_result

    # Build parameter paths
    username_path = _resolve_wan_path(root, "ppp.username", instance_index)
    password_path = _resolve_wan_path(root, "ppp.password", instance_index)
    enable_path = _resolve_wan_path(root, "ppp.enable", instance_index)

    params: dict[str, str] = {
        username_path: username,
        password_path: password,
        enable_path: "true",
    }

    # For TR-098, also set NAT and connection type
    if root == "InternetGatewayDevice":
        nat_path = _resolve_wan_path(root, "ppp.nat_enabled", instance_index)
        conn_type_path = _resolve_wan_path(root, "ppp.connection_type", instance_index)
        params[nat_path] = "true"
        params[conn_type_path] = "IP_Routed"

        # Set VLAN if provided
        if wan_vlan is not None:
            vlan_path = _resolve_wan_path(root, "ppp.vlan", instance_index)
            params[vlan_path] = str(wan_vlan)

            service_path = _resolve_wan_path(root, "ppp.service_list", instance_index)
            params[service_path] = "INTERNET"

    # Expected values for verification (exclude password - it's write-only)
    expected = {
        path: value
        for path, value in params.items()
        if not path.endswith(".Password")
    }

    try:
        result = set_and_verify(client, device_id, params, expected=expected)
        logger.info(
            "PPPoE credentials set on ONT %s (user: %s, instance: %d, root: %s)",
            ont.serial_number,
            username,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"PPPoE credentials set on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "username": username,
                "instance_index": instance_index,
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set PPPoE credentials failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set PPPoE credentials: {exc}",
        )


# ---------------------------------------------------------------------------
# DHCP WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_dhcp(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    ensure_instance: bool = True,
    wan_vlan: int | None = None,
) -> ActionResult:
    """Configure WAN interface for DHCP mode.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        instance_index: WAN instance index (default 1).
        ensure_instance: If True, create IP instance if missing (default True).
        wan_vlan: Optional VLAN to apply.

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    # Ensure IP instance exists if requested
    if ensure_instance:
        ensure_result = ensure_wan_instance(
            db,
            ont_id,
            instance_index=instance_index,
            wan_type="ip",
            wan_vlan=wan_vlan,
        )
        if not ensure_result.success:
            return ensure_result

    params: dict[str, str] = {}

    if root == "InternetGatewayDevice":
        enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        nat_path = _resolve_wan_path(root, "ip.nat_enabled", instance_index)
        conn_type_path = _resolve_wan_path(root, "ip.connection_type", instance_index)
        addr_type_path = _resolve_wan_path(root, "ip.addressing_type", instance_index)

        params = {
            enable_path: "true",
            nat_path: "true",
            conn_type_path: "IP_Routed",
            addr_type_path: "DHCP",
        }
    else:
        # TR-181
        ip_enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        dhcp_enable_path = _resolve_wan_path(root, "dhcp.enable", instance_index)

        params = {
            ip_enable_path: "true",
            dhcp_enable_path: "true",
        }

    try:
        result = set_and_verify(client, device_id, params)
        logger.info(
            "WAN DHCP configured on ONT %s (instance: %d, root: %s)",
            ont.serial_number,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"WAN DHCP configured on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "instance_index": instance_index,
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set WAN DHCP failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set WAN DHCP: {exc}",
        )


# ---------------------------------------------------------------------------
# Static IP WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_static(
    db: Session,
    ont_id: str,
    *,
    ip_address: str,
    subnet_mask: str,
    gateway: str,
    dns_servers: str | None = None,
    instance_index: int = 1,
) -> ActionResult:
    """Configure WAN interface with static IP.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ip_address: Static IP address.
        subnet_mask: Subnet mask.
        gateway: Default gateway.
        dns_servers: Optional comma-separated DNS servers.
        instance_index: WAN instance index (default 1).

    Returns:
        ActionResult indicating success/failure.
    """
    if not ip_address:
        return ActionResult(success=False, message="IP address is required.")
    if not subnet_mask:
        return ActionResult(success=False, message="Subnet mask is required.")
    if not gateway:
        return ActionResult(success=False, message="Gateway is required.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "InternetGatewayDevice":
        enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        address_path = _resolve_wan_path(root, "ip.address", instance_index)
        subnet_path = _resolve_wan_path(root, "ip.subnet", instance_index)
        gateway_path = _resolve_wan_path(root, "ip.gateway", instance_index)
        nat_path = _resolve_wan_path(root, "ip.nat_enabled", instance_index)
        conn_type_path = _resolve_wan_path(root, "ip.connection_type", instance_index)
        addr_type_path = _resolve_wan_path(root, "ip.addressing_type", instance_index)

        params = {
            enable_path: "true",
            address_path: ip_address,
            subnet_path: subnet_mask,
            gateway_path: gateway,
            nat_path: "true",
            conn_type_path: "IP_Routed",
            addr_type_path: "Static",
        }

        if dns_servers:
            dns_path = _resolve_wan_path(root, "ip.dns", instance_index)
            params[dns_path] = dns_servers
    else:
        # TR-181
        ip_enable_path = _resolve_wan_path(root, "ip.enable", instance_index)
        address_path = _resolve_wan_path(root, "ip.static_address", instance_index)
        subnet_path = _resolve_wan_path(root, "ip.static_subnet", instance_index)

        params = {
            ip_enable_path: "true",
            address_path: ip_address,
            subnet_path: subnet_mask,
        }

    try:
        result = set_and_verify(client, device_id, params)
        logger.info(
            "WAN static IP configured on ONT %s (ip: %s, instance: %d, root: %s)",
            ont.serial_number,
            ip_address,
            instance_index,
            root,
        )
        return ActionResult(
            success=True,
            message=f"WAN static IP ({ip_address}) configured on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "ip_address": ip_address,
                "instance_index": instance_index,
                "root": root,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set WAN static IP failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set WAN static IP: {exc}",
        )


# ---------------------------------------------------------------------------
# Unified WAN Configuration
# ---------------------------------------------------------------------------


def set_wan_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    instance_index: int = 1,
    wan_vlan: int | None = None,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: str | None = None,
    ensure_instance: bool = True,
) -> ActionResult:
    """Unified entry point for WAN configuration.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        wan_mode: WAN mode ("pppoe", "dhcp", "static", "bridge").
        instance_index: WAN instance index (default 1).
        wan_vlan: Optional VLAN to apply.
        pppoe_username: PPPoE username (required for pppoe mode).
        pppoe_password: PPPoE password (required for pppoe mode).
        ip_address: Static IP (required for static mode).
        subnet_mask: Subnet mask (required for static mode).
        gateway: Gateway (required for static mode).
        dns_servers: Optional DNS servers.
        ensure_instance: If True, create WAN instance if missing.

    Returns:
        ActionResult indicating success/failure.
    """
    wan_mode = wan_mode.lower().strip()

    if wan_mode == "pppoe":
        if not pppoe_username or not pppoe_password:
            return ActionResult(
                success=False,
                message="PPPoE username and password are required for PPPoE mode.",
            )
        return set_pppoe_credentials(
            db,
            ont_id,
            username=pppoe_username,
            password=pppoe_password,
            instance_index=instance_index,
            ensure_instance=ensure_instance,
            wan_vlan=wan_vlan,
        )

    elif wan_mode == "dhcp":
        return set_wan_dhcp(
            db,
            ont_id,
            instance_index=instance_index,
            ensure_instance=ensure_instance,
            wan_vlan=wan_vlan,
        )

    elif wan_mode == "static":
        if not ip_address or not subnet_mask or not gateway:
            return ActionResult(
                success=False,
                message="IP address, subnet mask, and gateway are required for static mode.",
            )
        return set_wan_static(
            db,
            ont_id,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            dns_servers=dns_servers,
            instance_index=instance_index,
        )

    elif wan_mode == "bridge":
        # Bridge mode typically doesn't need TR-069 WAN config
        return ActionResult(
            success=True,
            message="Bridge mode does not require TR-069 WAN configuration.",
            data={"wan_mode": "bridge", "skipped": True},
        )

    else:
        return ActionResult(
            success=False,
            message=f"Unknown WAN mode: {wan_mode}. Use pppoe, dhcp, static, or bridge.",
        )


# ---------------------------------------------------------------------------
# IPv6 Configuration
# ---------------------------------------------------------------------------


def set_wan_ipv6_config(
    db: Session,
    ont_id: str,
    *,
    ipv6_enabled: bool = True,
    dhcpv6_pd_enabled: bool = True,
    instance_index: int = 1,
) -> ActionResult:
    """Configure IPv6 settings on WAN interface.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ipv6_enabled: Enable IPv6 on the interface.
        dhcpv6_pd_enabled: Enable DHCPv6 prefix delegation.
        instance_index: WAN instance index (default 1).

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    params: dict[str, str] = {}

    if root == "Device":
        # TR-181 has proper IPv6 support
        ipv6_enable_path = _resolve_wan_path(root, "ip.ipv6_enable", instance_index)
        dhcpv6_enable_path = _resolve_wan_path(root, "dhcpv6.enable", instance_index)
        dhcpv6_pd_path = _resolve_wan_path(
            root, "dhcpv6.request_prefixes", instance_index
        )

        params = {
            ipv6_enable_path: "true" if ipv6_enabled else "false",
            dhcpv6_enable_path: "true" if ipv6_enabled else "false",
            dhcpv6_pd_path: "true" if dhcpv6_pd_enabled else "false",
        }
    else:
        # TR-098 IPv6 support is vendor-specific
        return ActionResult(
            success=False,
            message="IPv6 configuration via TR-069 is not supported on TR-098 devices.",
            data={"root": root, "unsupported": True},
        )

    try:
        result = set_and_verify(client, device_id, params)
        state = "enabled" if ipv6_enabled else "disabled"
        logger.info(
            "IPv6 %s on ONT %s (instance: %d)",
            state,
            ont.serial_number,
            instance_index,
        )
        return ActionResult(
            success=True,
            message=f"IPv6 {state} on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "ipv6_enabled": ipv6_enabled,
                "dhcpv6_pd_enabled": dhcpv6_pd_enabled,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set IPv6 config failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set IPv6 config: {exc}",
        )


# ---------------------------------------------------------------------------
# HTTP Management Toggle
# ---------------------------------------------------------------------------


def set_http_management(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    port: int = 80,
) -> ActionResult:
    """Enable or disable HTTP management interface on ONT.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        enabled: Enable or disable HTTP management.
        port: HTTP port (default 80).

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    http_paths = _HTTP_MGMT_PATHS.get(root, {})
    if not http_paths:
        return ActionResult(
            success=False,
            message="HTTP management paths not configured for this device type.",
        )

    enable_path = f"{root}.{http_paths['enable']}"
    port_path = f"{root}.{http_paths['port']}"

    params = {
        enable_path: "true" if enabled else "false",
        port_path: str(port),
    }

    try:
        result = set_and_verify(client, device_id, params)
        state = "enabled" if enabled else "disabled"
        logger.info(
            "HTTP management %s on ONT %s (port: %d)",
            state,
            ont.serial_number,
            port,
        )
        return ActionResult(
            success=True,
            message=f"HTTP management {state} on {ont.serial_number} (port {port}).",
            data={
                "device_id": device_id,
                "http_enabled": enabled,
                "http_port": port,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Set HTTP management failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to set HTTP management: {exc}",
        )


# ---------------------------------------------------------------------------
# WAN Instance Deletion
# ---------------------------------------------------------------------------


def delete_wan_instance(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_type: str = "ppp",
) -> ActionResult:
    """Delete a WAN instance via TR-069 deleteObject.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        instance_index: WAN instance index to delete.
        wan_type: Type of WAN interface ("ppp" or "ip").

    Returns:
        ActionResult indicating success/failure.
    """
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    if root == "InternetGatewayDevice":
        if wan_type == "ppp":
            object_path = (
                f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}."
                "WANPPPConnection.1."
            )
        else:
            object_path = (
                f"{root}.WANDevice.1.WANConnectionDevice.{instance_index}."
                "WANIPConnection.1."
            )
    else:
        # TR-181
        if wan_type == "ppp":
            object_path = f"{root}.PPP.Interface.{instance_index}."
        else:
            object_path = f"{root}.IP.Interface.{instance_index}."

    try:
        result = client.delete_object(device_id, object_path)
        logger.info(
            "WAN %s instance %d deleted on ONT %s",
            wan_type,
            instance_index,
            ont.serial_number,
        )
        return ActionResult(
            success=True,
            message=f"WAN {wan_type} instance {instance_index} deleted on {ont.serial_number}.",
            data={
                "device_id": device_id,
                "instance_index": instance_index,
                "wan_type": wan_type,
                "object_path": object_path,
                "task": result,
            },
        )
    except GenieACSError as exc:
        logger.error(
            "Delete WAN instance failed for ONT %s: %s",
            ont.serial_number,
            exc,
        )
        return ActionResult(
            success=False,
            message=f"Failed to delete WAN instance: {exc}",
        )


# ---------------------------------------------------------------------------
# WAN Structure Normalization
# ---------------------------------------------------------------------------


def _discover_wan_instances(
    client: Any,
    device: dict[str, Any],
    root: str,
) -> list[dict[str, Any]]:
    """Discover all WAN instances on an IGD device.

    Returns list of dicts with wcd_index, type (ppp/ip), service, vlan, is_mgmt.
    """
    instances: list[dict[str, Any]] = []

    if root != "InternetGatewayDevice":
        return instances

    wan_device = (
        device.get(root, {})
        .get("WANDevice", {})
        .get("1", {})
        .get("WANConnectionDevice", {})
    )
    if not isinstance(wan_device, dict):
        return instances

    for wcd_key in sorted(wan_device.keys(), key=lambda k: int(k) if k.isdigit() else 999):
        if not wcd_key.isdigit():
            continue
        wcd = wan_device[wcd_key]
        if not isinstance(wcd, dict):
            continue

        wcd_index = int(wcd_key)

        # Check PPP connections
        ppp_container = wcd.get("WANPPPConnection", {})
        if isinstance(ppp_container, dict):
            for conn_key in ppp_container.keys():
                if not conn_key.isdigit():
                    continue
                conn = ppp_container[conn_key]
                if not isinstance(conn, dict):
                    continue

                service = str(
                    client.extract_parameter_value(conn, "X_HW_SERVICELIST") or ""
                ).upper()
                vlan = client.extract_parameter_value(conn, "X_HW_VLAN")
                name = client.extract_parameter_value(conn, "Name") or ""
                is_mgmt = "TR069" in service or "MGMT" in service.upper() or "MANAGEMENT" in str(name).upper()

                instances.append({
                    "wcd_index": wcd_index,
                    "conn_index": int(conn_key),
                    "type": "ppp",
                    "service": service,
                    "vlan": vlan,
                    "name": name,
                    "is_mgmt": is_mgmt,
                })

        # Check IP connections
        ip_container = wcd.get("WANIPConnection", {})
        if isinstance(ip_container, dict):
            for conn_key in ip_container.keys():
                if not conn_key.isdigit():
                    continue
                conn = ip_container[conn_key]
                if not isinstance(conn, dict):
                    continue

                service = str(
                    client.extract_parameter_value(conn, "X_HW_SERVICELIST") or ""
                ).upper()
                vlan = client.extract_parameter_value(conn, "X_HW_VLAN")
                name = client.extract_parameter_value(conn, "Name") or ""
                is_mgmt = "TR069" in service or "MGMT" in service.upper() or "MANAGEMENT" in str(name).upper()

                instances.append({
                    "wcd_index": wcd_index,
                    "conn_index": int(conn_key),
                    "type": "ip",
                    "service": service,
                    "vlan": vlan,
                    "name": name,
                    "is_mgmt": is_mgmt,
                })

    return instances


def normalize_wan_structure(
    db: Session,
    ont_id: str,
    *,
    mgmt_vlan: int | None = None,
    internet_vlan: int | None = None,
    preserve_mgmt: bool = True,
) -> ActionResult:
    """Normalize WAN structure by removing non-management WAN instances.

    Deletes non-management WAN instances to prepare for fresh provisioning
    with a predictable layout:
    - WCD1 = Management (TR-069, static IP) - preserved by default
    - WCD2 = Internet (PPPoE/DHCP) - created during subsequent provisioning

    This ensures consistent TR-069 parameter paths across all ONTs regardless
    of how they were originally provisioned.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        mgmt_vlan: Management VLAN tag (from config pack if not specified).
        internet_vlan: Internet VLAN tag (from config pack if not specified).
        preserve_mgmt: If True, don't delete the management WAN (default True).

    Returns:
        ActionResult with normalization details.
    """
    from app.services.network.olt_config_pack import resolve_olt_config_pack

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    ont, client, device_id = resolved
    root = detect_data_model_root(db, ont, client, device_id)
    persist_data_model_root(ont, root)

    if root != "InternetGatewayDevice":
        return ActionResult(
            success=False,
            message="WAN normalization is only supported for TR-098 (IGD) devices.",
            data={"root": root, "unsupported": True},
        )

    # Get VLANs from config pack if not provided
    if mgmt_vlan is None or internet_vlan is None:
        olt_id = getattr(ont, "olt_device_id", None)
        if olt_id:
            config_pack = resolve_olt_config_pack(db, str(olt_id))
            if config_pack:
                if mgmt_vlan is None:
                    mgmt_vlan_obj = getattr(config_pack, "management_vlan", None)
                    if mgmt_vlan_obj and getattr(mgmt_vlan_obj, "tag", None):
                        mgmt_vlan = mgmt_vlan_obj.tag
                if internet_vlan is None:
                    internet_vlan_obj = getattr(config_pack, "internet_vlan", None)
                    if internet_vlan_obj and getattr(internet_vlan_obj, "tag", None):
                        internet_vlan = internet_vlan_obj.tag

    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        return ActionResult(success=False, message=f"Failed to fetch device: {exc}")

    # Discover existing WAN instances
    instances = _discover_wan_instances(client, device, root)

    if not instances:
        return ActionResult(
            success=True,
            message="No WAN instances found. Device may need OLT re-provisioning.",
            data={"instances_found": 0, "action": "none"},
        )

    deleted_count = 0
    preserved_count = 0
    errors: list[str] = []

    # Delete non-management instances (or all if preserve_mgmt=False)
    for instance in instances:
        if preserve_mgmt and instance["is_mgmt"]:
            logger.info(
                "Preserving management WAN on ONT %s: WCD%d %s (VLAN %s)",
                ont.serial_number,
                instance["wcd_index"],
                instance["service"],
                instance["vlan"],
            )
            preserved_count += 1
            continue

        # Build delete path
        wcd_idx = instance["wcd_index"]
        conn_idx = instance["conn_index"]
        wan_type = instance["type"]

        if wan_type == "ppp":
            delete_path = (
                f"{root}.WANDevice.1.WANConnectionDevice.{wcd_idx}."
                f"WANPPPConnection.{conn_idx}."
            )
        else:
            delete_path = (
                f"{root}.WANDevice.1.WANConnectionDevice.{wcd_idx}."
                f"WANIPConnection.{conn_idx}."
            )

        try:
            client.delete_object(device_id, delete_path)
            deleted_count += 1
            logger.info(
                "Deleted WAN instance on ONT %s: WCD%d/%s.%d (VLAN %s, service %s)",
                ont.serial_number,
                wcd_idx,
                wan_type,
                conn_idx,
                instance["vlan"],
                instance["service"],
            )
        except GenieACSError as exc:
            error_msg = f"Failed to delete WCD{wcd_idx}/{wan_type}.{conn_idx}: {exc}"
            errors.append(error_msg)
            logger.warning("WAN normalize error on ONT %s: %s", ont.serial_number, error_msg)

    # Record normalization state
    capabilities = _runtime_capabilities(ont)
    capabilities["wan_normalized"] = True
    capabilities["wan_normalized_at"] = _iso_now()
    _persist_runtime_capabilities(ont, capabilities)
    db.flush()

    if errors:
        return ActionResult(
            success=False,
            message=f"WAN normalization partially failed: {len(errors)} error(s).",
            data={
                "deleted": deleted_count,
                "preserved": preserved_count,
                "errors": errors,
                "instances_found": len(instances),
            },
        )

    return ActionResult(
        success=True,
        message=f"WAN structure normalized. Deleted {deleted_count}, preserved {preserved_count}.",
        data={
            "deleted": deleted_count,
            "preserved": preserved_count,
            "instances_found": len(instances),
            "mgmt_vlan": mgmt_vlan,
            "internet_vlan": internet_vlan,
        },
    )
