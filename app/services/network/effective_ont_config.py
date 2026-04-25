"""Resolve ONT desired state from OLT defaults plus ONT-local intent."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OLTDevice, OntUnit
from app.services.network.olt_config_pack import OltConfigPack, resolve_olt_config_pack
from app.services.network.ont_desired_config import desired_config


def _enum_or_raw(value: Any) -> Any:
    return getattr(value, "value", value)


def _first_present(*values: Any) -> Any:
    for value in values:
        # Check for None and empty string, but preserve False and 0
        if value is not None and value != "":
            return _enum_or_raw(value)
    return None


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


def _values_from_desired_config(
    ont: OntUnit,
    config_pack: OltConfigPack | None,
) -> dict[str, Any]:
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

    return {
        "config_method": _first_present(device.get("config_method")),
        "onu_mode": _first_present(device.get("onu_mode")),
        "ip_protocol": _first_present(wan.get("ip_protocol")),
        "wan_mode": _first_present(wan.get("mode")),
        "wan_vlan": _first_present(
            wan.get("vlan"),
            getattr(config_pack.internet_vlan, "tag", None) if config_pack else None,
        ),
        "pppoe_username": _first_present(wan.get("pppoe_username")),
        "pppoe_password": _first_present(wan.get("pppoe_password")),
        "wan_static_ip": _first_present(wan.get("static_ip"), wan.get("ip_address")),
        "wan_static_subnet": _first_present(wan.get("static_subnet"), wan.get("subnet")),
        "wan_static_gateway": _first_present(wan.get("static_gateway"), wan.get("gateway")),
        "wan_static_dns": _first_present(wan.get("static_dns"), wan.get("dns")),
        "wan_instance_index": _first_present(wan.get("instance_index"), 1),
        "wan_gem_index": _first_present(
            wan.get("gem_index"),
            config_pack.internet_gem_index if config_pack else None,
        ),
        "mgmt_ip_mode": _first_present(management.get("ip_mode")),
        "mgmt_vlan": _first_present(
            management.get("vlan"),
            getattr(config_pack.management_vlan, "tag", None) if config_pack else None,
        ),
        "mgmt_ip_address": _first_present(management.get("ip_address")),
        "mgmt_subnet": _first_present(management.get("subnet"), management.get("subnet_mask")),
        "mgmt_gateway": _first_present(management.get("gateway")),
        "lan_ip": _first_present(lan.get("ip"), lan.get("gateway_ip")),
        "lan_subnet": _first_present(lan.get("subnet"), lan.get("subnet_mask")),
        "lan_dhcp_enabled": _first_present(lan.get("dhcp_enabled")),
        "lan_dhcp_start": _first_present(lan.get("dhcp_start")),
        "lan_dhcp_end": _first_present(lan.get("dhcp_end")),
        "wifi_enabled": _first_present(wifi.get("enabled")),
        "wifi_ssid": _first_present(wifi.get("ssid")),
        "wifi_password": _first_present(wifi.get("password")),
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
    """Return ONT desired config resolved from OLT defaults and ONT intent."""
    config = desired_config(ont)
    config_pack = _resolve_config_pack(db, ont, olt=olt)
    return {
        "config_pack": config_pack,
        "desired_config_keys": _explicit_keys(config),
        "values": _values_from_desired_config(ont, config_pack),
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
