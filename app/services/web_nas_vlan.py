"""Web service for NAS VLAN management on the admin portal."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.services import web_admin as web_admin_service
from app.services.audit_helpers import log_audit_event

logger = logging.getLogger(__name__)


def vlan_list_context(db: Session, device_id: str) -> dict[str, Any]:
    """Build template context for the VLAN tab partial.

    Args:
        db: Database session.
        device_id: NAS device ID.

    Returns:
        Dict with device_id, vlans, ip_map, pppoe_map, and optional error.
    """
    device = db.get(NasDevice, device_id)
    if not device:
        return {"device_id": device_id, "vlans": [], "error": "Device not found"}

    from app.services.nas._mikrotik_vlan import (
        list_ip_addresses,
        list_pppoe_servers,
        list_vlan_interfaces,
    )

    vlans = list_vlan_interfaces(device)
    ips = list_ip_addresses(device)
    pppoe_list = list_pppoe_servers(device)

    ip_map: dict[str, str] = {}
    for ip in ips:
        iface = ip.get("interface") or ""
        if iface and not ip.get("disabled"):
            ip_map[iface] = ip.get("address") or ""

    pppoe_map: dict[str, str] = {}
    for srv in pppoe_list:
        iface = srv.get("interface") or ""
        if iface:
            pppoe_map[iface] = srv.get("service_name") or ""

    return {
        "device_id": device_id,
        "vlans": vlans,
        "ip_map": ip_map,
        "pppoe_map": pppoe_map,
    }


def validate_vlan_create(
    *,
    vlan_id: int,
    ip_address: str,
    parent_interface: str,
) -> str | None:
    """Validate VLAN creation parameters.

    Returns:
        Error message string if invalid, None if valid.
    """
    if vlan_id < 1 or vlan_id > 4094:
        return f"VLAN ID must be between 1 and 4094 (got {vlan_id})."

    if not parent_interface or not parent_interface.strip():
        return "Parent interface is required."

    try:
        ipaddress.IPv4Interface(ip_address)
    except (ValueError, ipaddress.AddressValueError):
        return (
            f"Invalid IP address/CIDR: {ip_address}. Expected format: 172.16.110.1/24"
        )

    return None


def handle_vlan_create(
    db: Session,
    device_id: str,
    *,
    vlan_id: int,
    parent_interface: str,
    ip_address: str,
    pppoe_service_name: str | None = None,
) -> dict[str, Any]:
    """Create a VLAN + IP + PPPoE server on a NAS device.

    Returns:
        Dict with success, message, and details.
    """
    device = db.get(NasDevice, device_id)
    if not device:
        return {"success": False, "message": "NAS device not found."}

    error = validate_vlan_create(
        vlan_id=vlan_id, ip_address=ip_address, parent_interface=parent_interface
    )
    if error:
        return {"success": False, "message": error}

    from app.services.nas._mikrotik_vlan import provision_vlan_full

    result = provision_vlan_full(
        device,
        vlan_id=vlan_id,
        parent_interface=parent_interface,
        ip_address=ip_address,
        pppoe_service_name=pppoe_service_name or None,
    )
    return {
        "success": result.success,
        "message": result.message,
        "details": result.details,
    }


def handle_vlan_create_with_audit(
    db: Session,
    *,
    request,
    device_id: str,
    vlan_id: int,
    parent_interface: str,
    ip_address: str,
    pppoe_service_name: str | None = None,
) -> dict[str, Any]:
    """Create a NAS VLAN and record the admin audit event."""
    result = handle_vlan_create(
        db,
        device_id,
        vlan_id=vlan_id,
        parent_interface=parent_interface,
        ip_address=ip_address,
        pppoe_service_name=pppoe_service_name,
    )
    current_user = web_admin_service.get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="create_vlan",
        entity_type="nas_device",
        entity_id=device_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "vlan_id": vlan_id,
            "parent_interface": parent_interface,
            "ip_address": ip_address,
            "success": result["success"],
            "message": result["message"],
        },
    )
    return result


def handle_vlan_delete(
    db: Session,
    device_id: str,
    *,
    vlan_id: int,
    parent_interface: str,
) -> dict[str, Any]:
    """Remove a VLAN interface from a NAS device.

    Returns:
        Dict with success and message.
    """
    device = db.get(NasDevice, device_id)
    if not device:
        return {"success": False, "message": "NAS device not found."}

    from app.services.nas._mikrotik_vlan import remove_vlan_interface

    result = remove_vlan_interface(
        device, vlan_id=vlan_id, parent_interface=parent_interface
    )
    return {"success": result.success, "message": result.message}


def handle_vlan_delete_with_audit(
    db: Session,
    *,
    request,
    device_id: str,
    vlan_id: int,
    parent_interface: str,
) -> dict[str, Any]:
    """Delete a NAS VLAN and record the admin audit event."""
    result = handle_vlan_delete(
        db,
        device_id,
        vlan_id=vlan_id,
        parent_interface=parent_interface,
    )
    current_user = web_admin_service.get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action="delete_vlan",
        entity_type="nas_device",
        entity_id=device_id,
        actor_id=str(current_user.get("subscriber_id")) if current_user else None,
        metadata={
            "vlan_id": vlan_id,
            "success": result["success"],
            "message": result["message"],
        },
    )
    return result
