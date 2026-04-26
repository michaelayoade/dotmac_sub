"""Resolve ONT desired state from OLT config pack plus OntAssignment service config.

Architecture:
- Network config (VLANs, profiles, GEM indices, TR-069) comes from OLT Config Pack
- Service config (WAN mode, IP mode, PPPoE, WiFi) comes from OntAssignment
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.services.network._util import first_present_enum as _first_present
from app.services.network.olt_config_pack import OltConfigPack, resolve_olt_config_pack
from app.services.network.ont_desired_config import desired_config


def _resolve_config_pack(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> OltConfigPack | None:
    if olt is not None:
        return resolve_olt_config_pack(db, olt.id)
    olt_id = getattr(ont, "olt_device_id", None)
    if olt_id is None:
        return None
    return resolve_olt_config_pack(db, olt_id)


def _get_active_assignment(ont: OntUnit) -> OntAssignment | None:
    """Get the active assignment for this ONT, if any."""
    for assignment in getattr(ont, "assignments", []):
        if getattr(assignment, "active", False):
            return assignment
    return None


def _values_from_assignment(
    config_pack: OltConfigPack | None,
    assignment: OntAssignment | None = None,
) -> dict[str, Any]:
    """Build effective config values from the two authoritative sources."""
    asn_wan_mode = None
    asn_ip_mode = None
    asn_static_ip = None
    asn_static_gateway = None
    asn_static_subnet = None
    asn_pppoe_username = None
    asn_pppoe_password = None
    asn_wifi_ssid = None
    asn_wifi_password = None
    if assignment is not None:
        # Get enum values as strings for compatibility
        if hasattr(assignment, "wan_mode") and assignment.wan_mode is not None:
            asn_wan_mode = assignment.wan_mode.value
        if hasattr(assignment, "ip_mode") and assignment.ip_mode is not None:
            asn_ip_mode = assignment.ip_mode.value
        asn_static_ip = getattr(assignment, "static_ip", None)
        asn_static_gateway = getattr(assignment, "static_gateway", None)
        asn_static_subnet = getattr(assignment, "static_subnet", None)
        asn_pppoe_username = getattr(assignment, "pppoe_username", None)
        asn_pppoe_password = getattr(assignment, "pppoe_password", None)
        if asn_pppoe_username:
            asn_ip_mode = "pppoe"
        elif asn_static_ip:
            asn_ip_mode = "static_ip"
        asn_wifi_ssid = getattr(assignment, "wifi_ssid", None)
        asn_wifi_password = getattr(assignment, "wifi_password", None)

    asn_mgmt_ip_mode = None
    asn_mgmt_ip_address = None
    if assignment is not None:
        mgmt_ip_mode = getattr(assignment, "mgmt_ip_mode", None)
        if mgmt_ip_mode is not None:
            asn_mgmt_ip_mode = mgmt_ip_mode.value if hasattr(mgmt_ip_mode, "value") else str(mgmt_ip_mode)
        asn_mgmt_ip_address = getattr(assignment, "mgmt_ip_address", None)

    return {
        "config_method": None,
        "onu_mode": _first_present(asn_wan_mode),
        "ip_protocol": None,
        "wan_mode": _first_present(asn_ip_mode),
        "wan_vlan": _first_present(
            getattr(config_pack.internet_vlan, "tag", None) if config_pack else None,
        ),
        "pppoe_username": _first_present(asn_pppoe_username),
        "pppoe_password": _first_present(asn_pppoe_password),
        "wan_static_ip": _first_present(
            asn_static_ip,
        ),
        "wan_static_subnet": _first_present(
            asn_static_subnet,
        ),
        "wan_static_gateway": _first_present(
            asn_static_gateway,
        ),
        "wan_static_dns": _first_present(
            getattr(assignment, "static_dns", None) if assignment is not None else None,
        ),
        "wan_instance_index": 1,
        "wan_gem_index": config_pack.internet_gem_index if config_pack else None,
        "mgmt_ip_mode": _first_present(asn_mgmt_ip_mode),
        "mgmt_vlan": _first_present(
            getattr(config_pack.management_vlan, "tag", None) if config_pack else None,
        ),
        "mgmt_ip_address": _first_present(
            asn_mgmt_ip_address,
        ),
        "mgmt_subnet": _first_present(
            getattr(assignment, "mgmt_subnet", None) if assignment is not None else None,
        ),
        "mgmt_gateway": _first_present(
            getattr(assignment, "mgmt_gateway", None) if assignment is not None else None,
        ),
        "lan_ip": _first_present(
            getattr(assignment, "lan_ip", None) if assignment is not None else None,
        ),
        "lan_subnet": _first_present(
            getattr(assignment, "lan_subnet", None) if assignment is not None else None,
        ),
        "lan_dhcp_enabled": _first_present(
            getattr(assignment, "lan_dhcp_enabled", None) if assignment is not None else None,
        ),
        "lan_dhcp_start": _first_present(
            getattr(assignment, "lan_dhcp_start", None) if assignment is not None else None,
        ),
        "lan_dhcp_end": _first_present(
            getattr(assignment, "lan_dhcp_end", None) if assignment is not None else None,
        ),
        "wifi_enabled": _first_present(
            getattr(assignment, "wifi_enabled", None) if assignment is not None else None,
            True if asn_wifi_ssid else None,
        ),
        "wifi_ssid": _first_present(asn_wifi_ssid),
        "wifi_password": _first_present(asn_wifi_password),
        "wifi_channel": _first_present(
            getattr(assignment, "wifi_channel", None) if assignment is not None else None,
        ),
        "wifi_security_mode": _first_present(
            getattr(assignment, "wifi_security_mode", None) if assignment is not None else None,
        ),
        "tr069_acs_server_id": (
            config_pack.tr069_acs_server_id if config_pack else None
        ),
        "tr069_olt_profile_id": (
            config_pack.tr069_olt_profile_id if config_pack else None
        ),
        "cr_username": config_pack.cr_username if config_pack else None,
        "cr_password": config_pack.cr_password if config_pack else None,
        "internet_config_ip_index": (
            config_pack.internet_config_ip_index if config_pack else None
        ),
        "wan_config_profile_id": (
            config_pack.wan_config_profile_id if config_pack else None
        ),
        "pppoe_omci_vlan": None,
        # TR-069 WCD indices (OLT-provisioning-specific, determines WANConnectionDevice.{i})
        "pppoe_wcd_index": config_pack.pppoe_wcd_index if config_pack else 2,
        "mgmt_wcd_index": config_pack.mgmt_wcd_index if config_pack else 1,
        "voip_wcd_index": config_pack.voip_wcd_index if config_pack else None,
        "authorization_line_profile_id": (
            config_pack.line_profile_id if config_pack else None
        ),
        "authorization_service_profile_id": (
            config_pack.service_profile_id if config_pack else None
        ),
        "primary_wan_service": None,
    }


def _explicit_keys(config: dict[str, Any]) -> list[str]:
    keys: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for child_key, child_value in sorted(value.items()):
                walk(f"{prefix}.{child_key}" if prefix else child_key, child_value)
            return
        keys.append(prefix)

    walk("", config)
    return keys


def resolve_effective_ont_config(
    db: Session,
    ont: OntUnit,
    *,
    olt: OLTDevice | None = None,
) -> dict[str, Any]:
    """Return ONT desired config resolved from OLT config pack and OntAssignment.

    Service config (WAN mode, IP mode, PPPoE, LAN, WiFi) is read from the active
    OntAssignment.

    Network config (VLANs, profiles, GEM indices, TR-069) comes from OltConfigPack.
    """
    config = desired_config(ont)
    config_pack = _resolve_config_pack(db, ont, olt=olt)
    assignment = _get_active_assignment(ont)
    return {
        "config_pack": config_pack,
        "assignment": assignment,
        "desired_config_keys": _explicit_keys(config),
        "values": _values_from_assignment(config_pack, assignment),
    }


def get_effective_value(
    db: Session,
    ont: OntUnit,
    key: str,
    *,
    olt: OLTDevice | None = None,
    default: Any = None,
) -> Any:
    """Convenience accessor for callers that need one resolved value."""
    resolved = resolve_effective_ont_config(db, ont, olt=olt)
    return resolved.get("values", {}).get(key, default)
