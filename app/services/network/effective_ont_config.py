"""Resolve ONT desired state from OLT config pack plus OntAssignment service config.

Architecture:
- Network config (VLANs, profiles, GEM indices, TR-069) comes from OLT Config Pack
- Service config (WAN mode, IP mode, PPPoE, WiFi) comes from OntAssignment
- Legacy: OntUnit.desired_config is still checked for backwards compatibility
  but OntAssignment values take precedence when present
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


def _values_from_desired_config(
    ont: OntUnit,
    config_pack: OltConfigPack | None,
    assignment: OntAssignment | None = None,
) -> dict[str, Any]:
    """Build effective config values.

    Priority order (highest to lowest):
    1. OntAssignment service config (WAN mode, IP mode, PPPoE, WiFi)
    2. OntUnit.desired_config (legacy, for backwards compatibility)
    3. OltConfigPack defaults (network settings)
    """
    config = desired_config(ont)
    wan = config.get("wan") if isinstance(config.get("wan"), dict) else {}
    wifi = config.get("wifi") if isinstance(config.get("wifi"), dict) else {}
    management = (
        config.get("management") if isinstance(config.get("management"), dict) else {}
    )
    device = config.get("device") if isinstance(config.get("device"), dict) else {}
    tr069 = config.get("tr069") if isinstance(config.get("tr069"), dict) else {}
    omci = config.get("omci") if isinstance(config.get("omci"), dict) else {}
    lan = config.get("lan") if isinstance(config.get("lan"), dict) else {}
    authorization = (
        config.get("authorization")
        if isinstance(config.get("authorization"), dict)
        else {}
    )

    # Extract service config from assignment (takes precedence over desired_config)
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
        asn_wifi_ssid = getattr(assignment, "wifi_ssid", None)
        asn_wifi_password = getattr(assignment, "wifi_password", None)

    # Get VLAN overrides from assignment
    asn_internet_vlan = None
    asn_mgmt_vlan = None
    asn_mgmt_ip_mode = None
    asn_mgmt_ip_address = None
    if assignment is not None:
        internet_vlan = getattr(assignment, "internet_vlan", None)
        if internet_vlan:
            asn_internet_vlan = getattr(internet_vlan, "tag", None)
        mgmt_vlan = getattr(assignment, "mgmt_vlan", None)
        if mgmt_vlan:
            asn_mgmt_vlan = getattr(mgmt_vlan, "tag", None)
        mgmt_ip_mode = getattr(assignment, "mgmt_ip_mode", None)
        if mgmt_ip_mode is not None:
            asn_mgmt_ip_mode = mgmt_ip_mode.value if hasattr(mgmt_ip_mode, "value") else str(mgmt_ip_mode)
        asn_mgmt_ip_address = getattr(assignment, "mgmt_ip_address", None)

    return {
        "config_method": _first_present(device.get("config_method")),
        # WAN mode: assignment > desired_config
        "onu_mode": _first_present(asn_wan_mode, device.get("onu_mode")),
        "ip_protocol": _first_present(wan.get("ip_protocol")),
        # WAN/IP mode: assignment > desired_config
        "wan_mode": _first_present(asn_ip_mode, wan.get("mode")),
        "wan_vlan": _first_present(
            asn_internet_vlan,
            wan.get("vlan"),
            getattr(config_pack.internet_vlan, "tag", None) if config_pack else None,
        ),
        # PPPoE: assignment > desired_config
        "pppoe_username": _first_present(asn_pppoe_username, wan.get("pppoe_username")),
        "pppoe_password": _first_present(asn_pppoe_password, wan.get("pppoe_password")),
        # Static IP: assignment > desired_config
        "wan_static_ip": _first_present(asn_static_ip, wan.get("static_ip"), wan.get("ip_address")),
        "wan_static_subnet": _first_present(asn_static_subnet, wan.get("static_subnet"), wan.get("subnet")),
        "wan_static_gateway": _first_present(asn_static_gateway, wan.get("static_gateway"), wan.get("gateway")),
        "wan_static_dns": _first_present(wan.get("static_dns"), wan.get("dns")),
        "wan_instance_index": _first_present(wan.get("instance_index"), 1),
        "wan_gem_index": _first_present(
            wan.get("gem_index"),
            config_pack.internet_gem_index if config_pack else None,
        ),
        "mgmt_ip_mode": _first_present(asn_mgmt_ip_mode, management.get("ip_mode")),
        "mgmt_vlan": _first_present(
            asn_mgmt_vlan,
            management.get("vlan"),
            getattr(config_pack.management_vlan, "tag", None) if config_pack else None,
        ),
        "mgmt_ip_address": _first_present(asn_mgmt_ip_address, management.get("ip_address")),
        "mgmt_subnet": _first_present(management.get("subnet"), management.get("subnet_mask")),
        "mgmt_gateway": _first_present(management.get("gateway")),
        "lan_ip": _first_present(lan.get("ip"), lan.get("gateway_ip")),
        "lan_subnet": _first_present(lan.get("subnet"), lan.get("subnet_mask")),
        "lan_dhcp_enabled": _first_present(lan.get("dhcp_enabled")),
        "lan_dhcp_start": _first_present(lan.get("dhcp_start")),
        "lan_dhcp_end": _first_present(lan.get("dhcp_end")),
        # WiFi: assignment > desired_config
        "wifi_enabled": _first_present(
            True if asn_wifi_ssid else None,
            wifi.get("enabled"),
        ),
        "wifi_ssid": _first_present(asn_wifi_ssid, wifi.get("ssid")),
        "wifi_password": _first_present(asn_wifi_password, wifi.get("password")),
        "wifi_channel": _first_present(wifi.get("channel")),
        "wifi_security_mode": _first_present(wifi.get("security_mode")),
        "tr069_acs_server_id": _first_present(
            tr069.get("acs_server_id"),
            config_pack.tr069_acs_server_id if config_pack else None,
        ),
        "tr069_olt_profile_id": _first_present(
            tr069.get("olt_profile_id"),
            config_pack.tr069_olt_profile_id if config_pack else None,
        ),
        "cr_username": _first_present(
            tr069.get("cr_username"),
            config_pack.cr_username if config_pack else None,
        ),
        "cr_password": _first_present(
            tr069.get("cr_password"),
            config_pack.cr_password if config_pack else None,
        ),
        "internet_config_ip_index": _first_present(
            omci.get("internet_config_ip_index"),
            config_pack.internet_config_ip_index if config_pack else None,
        ),
        "wan_config_profile_id": _first_present(
            omci.get("wan_config_profile_id"),
            config_pack.wan_config_profile_id if config_pack else None,
        ),
        "pppoe_omci_vlan": _first_present(omci.get("pppoe_vlan")),
        "authorization_line_profile_id": _first_present(
            authorization.get("line_profile_id"),
            config_pack.line_profile_id if config_pack else None,
        ),
        "authorization_service_profile_id": _first_present(
            authorization.get("service_profile_id"),
            config_pack.service_profile_id if config_pack else None,
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

    Service config (WAN mode, IP mode, PPPoE, WiFi) is read from the active
    OntAssignment when present, falling back to OntUnit.desired_config for
    backwards compatibility.

    Network config (VLANs, profiles, GEM indices, TR-069) comes from OltConfigPack.
    """
    config = desired_config(ont)
    config_pack = _resolve_config_pack(db, ont, olt=olt)
    assignment = _get_active_assignment(ont)
    return {
        "config_pack": config_pack,
        "assignment": assignment,
        "desired_config_keys": _explicit_keys(config),
        "values": _values_from_desired_config(ont, config_pack, assignment),
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
