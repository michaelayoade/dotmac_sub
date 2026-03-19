"""MikroTik VLAN and PPPoE server management via RouterOS API.

Provides idempotent functions to create/verify/remove VLAN interfaces,
IP addresses, and PPPoE server bindings on MikroTik NAS devices.
Used by the provisioning automation to configure NAS devices when
onboarding new fiber subscribers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from app.models.catalog import NasDevice
from app.services.nas._mikrotik import _mikrotik_routeros_auth

logger = logging.getLogger(__name__)


@dataclass
class VlanProvisioningResult:
    """Result of a VLAN provisioning operation."""

    success: bool
    message: str
    created: bool = False
    details: dict[str, Any] | None = None


def _get_api(device: NasDevice) -> Any:
    """Create a RouterOS API connection to the device.

    Returns the API object. Caller must disconnect the pool.
    """
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
    return pool


def list_vlan_interfaces(device: NasDevice) -> list[dict[str, Any]]:
    """List all VLAN interfaces on the device.

    Returns:
        List of dicts with name, vlan-id, interface (parent), disabled, running.
    """
    pool = _get_api(device)
    try:
        api = pool.get_api()
        raw = cast(Any, api.get_resource("/interface/vlan")).get()
        if not isinstance(raw, list):
            return []
        return [
            {
                "id": item.get("id") or item.get(".id"),
                "name": item.get("name"),
                "vlan_id": item.get("vlan-id") or item.get("vlan_id"),
                "interface": item.get("interface"),
                "disabled": str(item.get("disabled", "false")).lower() == "true",
                "running": str(item.get("running", "false")).lower() == "true",
            }
            for item in raw
            if isinstance(item, dict)
        ]
    finally:
        pool.disconnect()


def list_ip_addresses(device: NasDevice) -> list[dict[str, Any]]:
    """List all IP addresses on the device."""
    pool = _get_api(device)
    try:
        api = pool.get_api()
        raw = cast(Any, api.get_resource("/ip/address")).get()
        if not isinstance(raw, list):
            return []
        return [
            {
                "id": item.get("id") or item.get(".id"),
                "address": item.get("address"),
                "interface": item.get("interface"),
                "disabled": str(item.get("disabled", "false")).lower() == "true",
            }
            for item in raw
            if isinstance(item, dict)
        ]
    finally:
        pool.disconnect()


def list_pppoe_servers(device: NasDevice) -> list[dict[str, Any]]:
    """List all PPPoE server bindings on the device."""
    pool = _get_api(device)
    try:
        api = pool.get_api()
        raw = cast(Any, api.get_resource("/interface/pppoe-server/server")).get()
        if not isinstance(raw, list):
            return []
        return [
            {
                "id": item.get("id") or item.get(".id"),
                "service_name": item.get("service-name") or item.get("service_name"),
                "interface": item.get("interface"),
                "default_profile": item.get("default-profile") or item.get("default_profile"),
                "disabled": str(item.get("disabled", "false")).lower() == "true",
            }
            for item in raw
            if isinstance(item, dict)
        ]
    finally:
        pool.disconnect()


def ensure_vlan_interface(
    device: NasDevice,
    *,
    vlan_id: int,
    parent_interface: str,
    name: str | None = None,
) -> VlanProvisioningResult:
    """Ensure a VLAN interface exists on the device. Creates if missing.

    Args:
        device: NAS device to configure.
        vlan_id: VLAN ID (1-4094).
        parent_interface: Parent physical interface (e.g., 'ether3', 'sfp1').
        name: Optional interface name. Defaults to 'vlan{vlan_id}'.

    Returns:
        VlanProvisioningResult with success/created status.
    """
    if vlan_id < 1 or vlan_id > 4094:
        return VlanProvisioningResult(
            success=False, message=f"Invalid VLAN ID: {vlan_id}"
        )

    iface_name = name or f"vlan{vlan_id}"
    pool = _get_api(device)
    try:
        api = pool.get_api()
        vlan_resource = cast(Any, api.get_resource("/interface/vlan"))

        # Check if VLAN already exists (by vlan-id + parent)
        existing = vlan_resource.get()
        for item in existing if isinstance(existing, list) else []:
            if not isinstance(item, dict):
                continue
            existing_vid = str(item.get("vlan-id") or item.get("vlan_id") or "")
            existing_parent = str(item.get("interface") or "")
            if existing_vid == str(vlan_id) and existing_parent == parent_interface:
                logger.info(
                    "VLAN %d already exists on %s/%s as '%s'",
                    vlan_id, device.name, parent_interface,
                    item.get("name"),
                )
                return VlanProvisioningResult(
                    success=True,
                    message=f"VLAN {vlan_id} already exists on {parent_interface}.",
                    created=False,
                    details={"name": item.get("name"), "vlan_id": vlan_id},
                )

        # Create VLAN interface
        vlan_resource.add(
            name=iface_name,
            **{"vlan-id": str(vlan_id)},
            interface=parent_interface,
        )
        logger.info(
            "Created VLAN %d (%s) on %s/%s",
            vlan_id, iface_name, device.name, parent_interface,
        )
        return VlanProvisioningResult(
            success=True,
            message=f"VLAN {vlan_id} ({iface_name}) created on {parent_interface}.",
            created=True,
            details={"name": iface_name, "vlan_id": vlan_id},
        )
    except Exception as exc:
        logger.error(
            "Failed to create VLAN %d on %s: %s", vlan_id, device.name, exc
        )
        return VlanProvisioningResult(
            success=False, message=f"Failed to create VLAN: {exc}"
        )
    finally:
        pool.disconnect()


def ensure_vlan_ip_address(
    device: NasDevice,
    *,
    interface_name: str,
    address: str,
) -> VlanProvisioningResult:
    """Ensure an IP address is assigned to an interface. Creates if missing.

    Args:
        device: NAS device to configure.
        interface_name: Interface name (e.g., 'vlan203').
        address: IP address with CIDR (e.g., '172.16.110.1/24').

    Returns:
        VlanProvisioningResult with success/created status.
    """
    pool = _get_api(device)
    try:
        api = pool.get_api()
        ip_resource = cast(Any, api.get_resource("/ip/address"))

        # Check if address already exists on interface
        existing = ip_resource.get()
        for item in existing if isinstance(existing, list) else []:
            if not isinstance(item, dict):
                continue
            existing_addr = str(item.get("address") or "")
            existing_iface = str(item.get("interface") or "")
            if existing_addr == address and existing_iface == interface_name:
                logger.info(
                    "IP %s already assigned to %s on %s",
                    address, interface_name, device.name,
                )
                return VlanProvisioningResult(
                    success=True,
                    message=f"IP {address} already assigned to {interface_name}.",
                    created=False,
                )

        # Add IP address
        ip_resource.add(address=address, interface=interface_name)
        logger.info(
            "Assigned IP %s to %s on %s",
            address, interface_name, device.name,
        )
        return VlanProvisioningResult(
            success=True,
            message=f"IP {address} assigned to {interface_name}.",
            created=True,
        )
    except Exception as exc:
        logger.error(
            "Failed to assign IP %s to %s on %s: %s",
            address, interface_name, device.name, exc,
        )
        return VlanProvisioningResult(
            success=False, message=f"Failed to assign IP: {exc}"
        )
    finally:
        pool.disconnect()


def ensure_pppoe_server(
    device: NasDevice,
    *,
    interface_name: str,
    service_name: str | None = None,
    default_profile: str = "default",
) -> VlanProvisioningResult:
    """Ensure a PPPoE server is bound to an interface. Creates if missing.

    Args:
        device: NAS device to configure.
        interface_name: Interface to bind PPPoE server to (e.g., 'vlan203').
        service_name: PPPoE service name. Defaults to 'pppoe-{interface_name}'.
        default_profile: PPP profile for new connections. Defaults to 'default'.

    Returns:
        VlanProvisioningResult with success/created status.
    """
    svc_name = service_name or f"pppoe-{interface_name}"
    pool = _get_api(device)
    try:
        api = pool.get_api()
        pppoe_resource = cast(Any, api.get_resource("/interface/pppoe-server/server"))

        # Check if PPPoE server already bound to this interface
        existing = pppoe_resource.get()
        for item in existing if isinstance(existing, list) else []:
            if not isinstance(item, dict):
                continue
            existing_iface = str(item.get("interface") or "")
            if existing_iface == interface_name:
                existing_svc = item.get("service-name") or item.get("service_name")
                logger.info(
                    "PPPoE server already bound to %s on %s (service: %s)",
                    interface_name, device.name, existing_svc,
                )
                return VlanProvisioningResult(
                    success=True,
                    message=f"PPPoE server already bound to {interface_name}.",
                    created=False,
                    details={"service_name": existing_svc},
                )

        # Create PPPoE server binding
        pppoe_resource.add(
            **{"service-name": svc_name},
            interface=interface_name,
            **{"default-profile": default_profile},
        )
        logger.info(
            "Created PPPoE server '%s' on %s/%s (profile: %s)",
            svc_name, device.name, interface_name, default_profile,
        )
        return VlanProvisioningResult(
            success=True,
            message=f"PPPoE server '{svc_name}' created on {interface_name}.",
            created=True,
            details={"service_name": svc_name, "default_profile": default_profile},
        )
    except Exception as exc:
        logger.error(
            "Failed to create PPPoE server on %s/%s: %s",
            device.name, interface_name, exc,
        )
        return VlanProvisioningResult(
            success=False, message=f"Failed to create PPPoE server: {exc}"
        )
    finally:
        pool.disconnect()


def provision_vlan_full(
    device: NasDevice,
    *,
    vlan_id: int,
    parent_interface: str,
    ip_address: str,
    vlan_name: str | None = None,
    pppoe_service_name: str | None = None,
    pppoe_default_profile: str = "default",
) -> VlanProvisioningResult:
    """Provision a complete VLAN + IP + PPPoE server in one call.

    Orchestrates ensure_vlan_interface → ensure_vlan_ip_address →
    ensure_pppoe_server. Stops on first failure.

    Args:
        device: NAS device to configure.
        vlan_id: VLAN ID (1-4094).
        parent_interface: Parent physical interface.
        ip_address: IP address with CIDR (e.g., '172.16.110.1/24').
        vlan_name: Optional VLAN interface name.
        pppoe_service_name: Optional PPPoE service name.
        pppoe_default_profile: PPP profile for new connections.

    Returns:
        VlanProvisioningResult summarizing all operations.
    """
    iface_name = vlan_name or f"vlan{vlan_id}"
    created_items: list[str] = []

    # Step 1: VLAN interface
    result = ensure_vlan_interface(
        device, vlan_id=vlan_id, parent_interface=parent_interface, name=iface_name
    )
    if not result.success:
        return result
    if result.created:
        created_items.append(f"VLAN {vlan_id}")

    # Step 2: IP address
    result = ensure_vlan_ip_address(
        device, interface_name=iface_name, address=ip_address
    )
    if not result.success:
        if created_items:
            result.message += f" (Note: {', '.join(created_items)} were created before this failure.)"
        return result
    if result.created:
        created_items.append(f"IP {ip_address}")

    # Step 3: PPPoE server
    result = ensure_pppoe_server(
        device,
        interface_name=iface_name,
        service_name=pppoe_service_name,
        default_profile=pppoe_default_profile,
    )
    if not result.success:
        if created_items:
            result.message += f" (Note: {', '.join(created_items)} were created before this failure.)"
        return result
    if result.created:
        created_items.append("PPPoE server")

    if created_items:
        msg = f"Provisioned on {device.name}: {', '.join(created_items)}."
    else:
        msg = f"All VLAN {vlan_id} components already exist on {device.name}."

    return VlanProvisioningResult(
        success=True,
        message=msg,
        created=bool(created_items),
        details={
            "vlan_id": vlan_id,
            "interface_name": iface_name,
            "ip_address": ip_address,
            "created": created_items,
        },
    )


def remove_vlan_interface(
    device: NasDevice,
    *,
    vlan_id: int,
    parent_interface: str,
) -> VlanProvisioningResult:
    """Remove a VLAN interface and its associated PPPoE server and IP.

    Removes in reverse order: PPPoE server → IP address → VLAN interface.

    Args:
        device: NAS device to configure.
        vlan_id: VLAN ID to remove.
        parent_interface: Parent interface the VLAN is on.

    Returns:
        VlanProvisioningResult with success status.
    """
    pool = _get_api(device)
    try:
        api = pool.get_api()

        # Find the VLAN interface
        vlan_resource = cast(Any, api.get_resource("/interface/vlan"))
        vlans = vlan_resource.get()
        target_vlan = None
        for item in vlans if isinstance(vlans, list) else []:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("vlan-id") or item.get("vlan_id") or "")
            parent = str(item.get("interface") or "")
            if vid == str(vlan_id) and parent == parent_interface:
                target_vlan = item
                break

        if not target_vlan:
            return VlanProvisioningResult(
                success=True,
                message=f"VLAN {vlan_id} not found on {parent_interface} — nothing to remove.",
            )

        vlan_name = str(target_vlan.get("name") or "")
        vlan_row_id = target_vlan.get("id") or target_vlan.get(".id")

        # Remove PPPoE servers on this interface
        pppoe_resource = cast(Any, api.get_resource("/interface/pppoe-server/server"))
        pppoe_list = pppoe_resource.get()
        for item in pppoe_list if isinstance(pppoe_list, list) else []:
            if isinstance(item, dict) and str(item.get("interface") or "") == vlan_name:
                row_id = item.get("id") or item.get(".id")
                if row_id:
                    pppoe_resource.remove(id=row_id)
                    logger.info("Removed PPPoE server on %s/%s", device.name, vlan_name)

        # Remove IP addresses on this interface
        ip_resource = cast(Any, api.get_resource("/ip/address"))
        ip_list = ip_resource.get()
        for item in ip_list if isinstance(ip_list, list) else []:
            if isinstance(item, dict) and str(item.get("interface") or "") == vlan_name:
                row_id = item.get("id") or item.get(".id")
                if row_id:
                    ip_resource.remove(id=row_id)
                    logger.info("Removed IP on %s/%s", device.name, vlan_name)

        # Remove VLAN interface
        if vlan_row_id:
            vlan_resource.remove(id=vlan_row_id)
            logger.info(
                "Removed VLAN %d (%s) from %s/%s",
                vlan_id, vlan_name, device.name, parent_interface,
            )

        return VlanProvisioningResult(
            success=True,
            message=f"VLAN {vlan_id} ({vlan_name}) removed from {device.name}.",
            details={"vlan_id": vlan_id, "name": vlan_name},
        )
    except Exception as exc:
        logger.error(
            "Failed to remove VLAN %d from %s: %s", vlan_id, device.name, exc
        )
        return VlanProvisioningResult(
            success=False, message=f"Failed to remove VLAN: {exc}"
        )
    finally:
        pool.disconnect()


def get_vlan_status(
    device: NasDevice,
    *,
    vlan_id: int,
    parent_interface: str,
) -> dict[str, Any]:
    """Check provisioning status for a specific VLAN on the device.

    Returns:
        Dict with has_vlan, has_ip, has_pppoe, and details for each.
    """
    iface_name: str | None = None
    result: dict[str, Any] = {
        "vlan_id": vlan_id,
        "has_vlan": False,
        "has_ip": False,
        "has_pppoe": False,
        "vlan_name": None,
        "ip_address": None,
        "pppoe_service": None,
    }

    pool = _get_api(device)
    try:
        api = pool.get_api()

        # Check VLAN
        vlans = cast(Any, api.get_resource("/interface/vlan")).get()
        for item in vlans if isinstance(vlans, list) else []:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("vlan-id") or item.get("vlan_id") or "")
            parent = str(item.get("interface") or "")
            if vid == str(vlan_id) and parent == parent_interface:
                iface_name = str(item.get("name") or "")
                result["has_vlan"] = True
                result["vlan_name"] = iface_name
                break

        if not iface_name:
            return result

        # Check IP
        ips = cast(Any, api.get_resource("/ip/address")).get()
        for item in ips if isinstance(ips, list) else []:
            if isinstance(item, dict) and str(item.get("interface") or "") == iface_name:
                result["has_ip"] = True
                result["ip_address"] = item.get("address")
                break

        # Check PPPoE server
        pppoe_list = cast(Any, api.get_resource("/interface/pppoe-server/server")).get()
        for item in pppoe_list if isinstance(pppoe_list, list) else []:
            if isinstance(item, dict) and str(item.get("interface") or "") == iface_name:
                result["has_pppoe"] = True
                result["pppoe_service"] = item.get("service-name") or item.get("service_name")
                break

        return result
    except Exception as exc:
        logger.error("Failed to check VLAN status on %s: %s", device.name, exc)
        result["error"] = str(exc)
        return result
    finally:
        pool.disconnect()
