"""Resolve ONT desired state from OLT config pack plus OntAssignment service config.

Architecture:
- Network config (VLANs, profiles, GEM indices, TR-069) comes from OLT Config Pack
- Service config (WAN mode, IP mode, PPPoE, WiFi) comes from OntAssignment
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.services.network.olt_config_pack import OltConfigPack, resolve_olt_config_pack
from app.services.network.ont_desired_config import desired_config, get_desired_config_value


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
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build effective config values from desired_config plus OLT defaults."""
    _ = assignment
    config = config or {}

    def cfg(*path: str, default: Any = None) -> Any:
        return get_desired_config_value(config, *path, default=default)

    asn_wan_mode = cfg("wan", "onu_mode")
    asn_ip_mode = cfg("wan", "mode")
    asn_static_ip = cfg("wan", "static_ip")
    asn_static_gateway = cfg("wan", "static_gateway")
    asn_static_subnet = cfg("wan", "static_subnet")
    asn_static_dns = cfg("wan", "static_dns")
    asn_pppoe_username = cfg("wan", "pppoe_username")
    asn_pppoe_password = cfg("wan", "pppoe_password")
    asn_wifi_ssid = cfg("wifi", "ssid")
    asn_wifi_password = cfg("wifi", "password")

    if asn_wan_mode is None and asn_ip_mode:
        asn_wan_mode = (
            "bridging"
            if str(asn_ip_mode) in {"bridge", "setup_via_onu"}
            else "routing"
        )

    asn_mgmt_ip_mode = cfg("management", "ip_mode")
    asn_mgmt_ip_address = cfg("management", "ip_address")
    asn_mgmt_subnet = cfg("management", "subnet")
    asn_mgmt_gateway = cfg("management", "gateway")

    asn_lan_ip = cfg("lan", "ip")
    asn_lan_subnet = cfg("lan", "subnet")
    asn_lan_dhcp_enabled = cfg("lan", "dhcp_enabled")
    asn_lan_dhcp_start = cfg("lan", "dhcp_start")
    asn_lan_dhcp_end = cfg("lan", "dhcp_end")
    asn_wifi_enabled = cfg("wifi", "enabled")
    asn_wifi_channel = cfg("wifi", "channel")
    asn_wifi_security_mode = cfg("wifi", "security_mode")
    # wifi_enabled: explicit setting takes precedence, else True if SSID is set
    wifi_enabled = (
        asn_wifi_enabled
        if asn_wifi_enabled is not None
        else (True if asn_wifi_ssid else None)
    )

    # Extract VLAN info (both ID and tag for compatibility)
    wan_vlan = config_pack.internet_vlan if config_pack else None
    mgmt_vlan = config_pack.management_vlan if config_pack else None

    return {
        "config_method": None,
        "onu_mode": asn_wan_mode,
        "ip_protocol": None,
        "wan_mode": asn_ip_mode,
        "wan_vlan": wan_vlan.tag if wan_vlan else None,
        "wan_vlan_id": str(wan_vlan.id) if wan_vlan and wan_vlan.id else None,
        "pppoe_username": asn_pppoe_username,
        "pppoe_password": asn_pppoe_password,
        "wan_static_ip": asn_static_ip,
        "wan_static_subnet": asn_static_subnet,
        "wan_static_gateway": asn_static_gateway,
        "wan_static_dns": asn_static_dns,
        "wan_instance_index": cfg("wan", "instance_index", default=1),
        "wan_gem_index": config_pack.internet_gem_index if config_pack else None,
        "mgmt_ip_mode": asn_mgmt_ip_mode,
        "mgmt_vlan": mgmt_vlan.tag if mgmt_vlan else None,
        "mgmt_vlan_id": str(mgmt_vlan.id) if mgmt_vlan and mgmt_vlan.id else None,
        "mgmt_ip_address": asn_mgmt_ip_address,
        "mgmt_subnet": asn_mgmt_subnet,
        "mgmt_gateway": asn_mgmt_gateway,
        "lan_ip": asn_lan_ip,
        "lan_subnet": asn_lan_subnet,
        "lan_dhcp_enabled": asn_lan_dhcp_enabled,
        "lan_dhcp_start": asn_lan_dhcp_start,
        "lan_dhcp_end": asn_lan_dhcp_end,
        "wifi_enabled": wifi_enabled,
        "wifi_ssid": asn_wifi_ssid,
        "wifi_password": asn_wifi_password,
        "wifi_channel": asn_wifi_channel,
        "wifi_security_mode": asn_wifi_security_mode,
        "tr069_acs_server_id": config_pack.tr069_acs_server_id if config_pack else None,
        "tr069_olt_profile_id": config_pack.tr069_olt_profile_id if config_pack else None,
        "cr_username": config_pack.cr_username if config_pack else None,
        "cr_password": config_pack.cr_password if config_pack else None,
        "internet_config_ip_index": config_pack.internet_config_ip_index if config_pack else None,
        "wan_config_profile_id": config_pack.wan_config_profile_id if config_pack else None,
        "pppoe_omci_vlan": None,
        # TR-069 WCD indices (OLT-provisioning-specific, determines WANConnectionDevice.{i})
        "pppoe_wcd_index": config_pack.pppoe_wcd_index if config_pack else 2,
        "mgmt_wcd_index": config_pack.mgmt_wcd_index if config_pack else 1,
        "voip_wcd_index": config_pack.voip_wcd_index if config_pack else None,
        "authorization_line_profile_id": config_pack.line_profile_id if config_pack else None,
        "authorization_service_profile_id": config_pack.service_profile_id if config_pack else None,
        "primary_wan_service": cfg("wan", "primary_service"),
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
        "values": _values_from_assignment(config_pack, assignment, config),
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
