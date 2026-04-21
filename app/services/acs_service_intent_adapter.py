"""Adapter for ACS/TR-069 observed service intent data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.services.adapters import adapter_registry

_UNSET_DISPLAY = "Not observed"


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _summary_attr(summary: object, name: str, default: object = None) -> object:
    if isinstance(summary, Mapping):
        return summary.get(name, default)
    return getattr(summary, name, default)


def _normalize_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _first_value(group: Mapping[str, Any], *keys: str) -> Any:
    normalized = {_normalize_key(str(key)): value for key, value in group.items()}
    for key in keys:
        if key in group and group[key] not in (None, ""):
            return group[key]
        value = normalized.get(_normalize_key(key))
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        if "_value" in value:
            value = value.get("_value")
        elif value and all(str(key).startswith("_") for key in value):
            return None
        else:
            return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _display(value: object) -> str:
    if value is None or value == "":
        return _UNSET_DISPLAY
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return str(value)


def _bool_value(value: object) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "up"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "down"}:
        return False
    return None


def _has_secret(value: object) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _row(label: str, value: object) -> dict[str, object]:
    return {"label": label, "value": _display(value)}


def _missing_count(*groups: Mapping[str, object]) -> int:
    count = 0
    for group in groups:
        for value in group.values():
            if value is None or value == "":
                count += 1
    return count


def _count_active(values: list[dict[str, object]], key: str = "active") -> int:
    return sum(1 for value in values if value.get(key) is True)


def _count_link_up_ports(values: list[dict[str, object]]) -> int:
    return sum(
        1
        for value in values
        if str(value.get("link_status") or "").strip().lower() == "up"
    )


class AcsServiceIntentAdapter:
    """Normalize ACS observed state into service-intent-shaped UI data."""

    name = "acs.service_intent"

    def load_observed_intent_for_ont(
        self, db: Session, *, ont_id: str
    ) -> dict[str, object]:
        from app.services.acs_client import create_acs_state_reader

        reader = create_acs_state_reader()
        summary = reader.get_device_summary(
            db,
            ont_id,
            persist_observed_runtime=True,
        )
        return self.build_observed_intent(summary)

    def build_observed_intent(self, summary: object | None) -> dict[str, object]:
        if summary is None:
            return self._unavailable(error="No ACS summary was provided.")

        available = bool(_summary_attr(summary, "available", False))
        error = _summary_attr(summary, "error")
        source = _text(_summary_attr(summary, "source")) or "unknown"
        fetched_at = _summary_attr(summary, "fetched_at")

        system_group = _as_mapping(_summary_attr(summary, "system", {}))
        wan_group = _as_mapping(_summary_attr(summary, "wan", {}))
        lan_group = _as_mapping(_summary_attr(summary, "lan", {}))
        wireless_group = _as_mapping(_summary_attr(summary, "wireless", {}))
        raw_ethernet_ports = list(_summary_attr(summary, "ethernet_ports", []) or [])
        raw_lan_hosts = list(_summary_attr(summary, "lan_hosts", []) or [])

        system = self._map_system(system_group)
        wan = self._map_wan(wan_group)
        lan = self._map_lan(lan_group)
        wifi = self._map_wifi(wireless_group)
        ethernet_ports = self._map_ethernet_ports(
            raw_ethernet_ports
        )
        lan_hosts = self._map_lan_hosts(
            raw_lan_hosts
        )
        sections = self._tracked_sections(
            system=system,
            wan=wan,
            lan=lan,
            wifi=wifi,
            ethernet_ports=ethernet_ports,
            lan_hosts=lan_hosts,
        )
        tracked_points = [
            dict(point)
            for section in sections
            for point in section.get("rows", [])
            if isinstance(point, Mapping)
        ]
        tracked_point_index = {
            str(point["key"]): dict(point)
            for point in tracked_points
            if point.get("key")
        }

        observed = {
            "system": system,
            "wan": wan,
            "lan": lan,
            "wifi": wifi,
            "ethernet_ports": ethernet_ports,
            "lan_hosts": lan_hosts,
        }
        missing_count = _missing_count(system, wan, lan, wifi)
        has_observed_data = bool(
            system_group
            or wan_group
            or lan_group
            or wireless_group
            or raw_ethernet_ports
            or raw_lan_hosts
            or fetched_at
        )
        sections_payload = sections if (available or has_observed_data) else []
        tracked_points_payload = tracked_points if (available or has_observed_data) else []
        tracked_point_index_payload = (
            tracked_point_index if (available or has_observed_data) else {}
        )

        return {
            "available": available,
            "source": source,
            "fetched_at": fetched_at,
            "error": _text(error),
            "observed": observed,
            "sections": sections_payload,
            "tracked_points": tracked_points_payload,
            "tracked_point_index": tracked_point_index_payload,
            "missing_count": missing_count,
            "is_complete": (available or has_observed_data) and missing_count == 0,
        }

    def _unavailable(self, *, error: str | None = None) -> dict[str, object]:
        return {
            "available": False,
            "source": "none",
            "fetched_at": None,
            "error": error,
            "observed": {
                "system": {},
                "wan": {},
                "lan": {},
                "wifi": {},
                "ethernet_ports": [],
                "lan_hosts": [],
            },
            "sections": [],
            "tracked_points": [],
            "tracked_point_index": {},
            "missing_count": 0,
            "is_complete": False,
        }

    def _tracked_sections(
        self,
        *,
        system: Mapping[str, object],
        wan: Mapping[str, object],
        lan: Mapping[str, object],
        wifi: Mapping[str, object],
        ethernet_ports: list[dict[str, object]],
        lan_hosts: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        def tracked_row(
            section_key: str,
            key: str,
            label: str,
            value: object,
        ) -> dict[str, object]:
            return {
                "section_key": section_key,
                "key": key,
                "label": label,
                "raw_value": value,
                "value": _display(value),
                "observed": value not in (None, ""),
            }

        return [
            {
                "key": "system",
                "title": "ACS System",
                "rows": [
                    tracked_row("system", "system.manufacturer", "Manufacturer", system["manufacturer"]),
                    tracked_row("system", "system.model", "Model", system["model"]),
                    tracked_row("system", "system.firmware", "Firmware", system["firmware"]),
                    tracked_row("system", "system.hardware", "Hardware", system["hardware"]),
                    tracked_row("system", "system.serial", "Serial", system["serial"]),
                    tracked_row("system", "system.uptime", "Uptime", system["uptime"]),
                    tracked_row("system", "system.cpu_usage", "CPU Usage", system["cpu_usage"]),
                    tracked_row("system", "system.memory_total", "Memory Total", system["memory_total"]),
                    tracked_row("system", "system.memory_free", "Memory Free", system["memory_free"]),
                    tracked_row("system", "system.memory_usage", "Memory Usage", system["memory_usage"]),
                    tracked_row("system", "system.mac_address", "MAC Address", system["mac_address"]),
                ],
            },
            {
                "key": "wan",
                "title": "ACS WAN",
                "rows": [
                    tracked_row("wan", "wan.connection_type", "Connection", wan["connection_type"]),
                    tracked_row("wan", "wan.status", "Status", wan["status"]),
                    tracked_row("wan", "wan.wan_ip", "WAN IP", wan["wan_ip"]),
                    tracked_row("wan", "wan.pppoe_username", "PPPoE User", wan["pppoe_username"]),
                    tracked_row("wan", "wan.gateway", "Gateway", wan["gateway"]),
                    tracked_row("wan", "wan.dns_servers", "DNS", wan["dns_servers"]),
                    tracked_row("wan", "wan.uptime", "Uptime", wan["uptime"]),
                    tracked_row("wan", "wan.wan_instance", "WAN Instance", wan["wan_instance"]),
                    tracked_row("wan", "wan.wan_service", "WAN Service", wan["wan_service"]),
                    tracked_row("wan", "wan.management_wan_ip", "Management WAN IP", wan["management_wan_ip"]),
                    tracked_row("wan", "wan.management_wan_status", "Management WAN Status", wan["management_wan_status"]),
                ],
            },
            {
                "key": "lan",
                "title": "ACS LAN",
                "rows": [
                    tracked_row("lan", "lan.lan_ip", "LAN IP", lan["lan_ip"]),
                    tracked_row("lan", "lan.subnet_mask", "Subnet", lan["subnet_mask"]),
                    tracked_row("lan", "lan.dhcp_enabled", "DHCP", lan["dhcp_enabled"]),
                    tracked_row("lan", "lan.dhcp_start", "DHCP Start", lan["dhcp_start"]),
                    tracked_row("lan", "lan.dhcp_end", "DHCP End", lan["dhcp_end"]),
                    tracked_row("lan", "lan.connected_hosts", "Hosts", lan["connected_hosts"]),
                ],
            },
            {
                "key": "wifi",
                "title": "ACS WiFi",
                "rows": [
                    tracked_row("wifi", "wifi.enabled", "Enabled", wifi["enabled"]),
                    tracked_row("wifi", "wifi.ssid", "SSID", wifi["ssid"]),
                    tracked_row("wifi", "wifi.channel", "Channel", wifi["channel"]),
                    tracked_row("wifi", "wifi.standard", "Standard", wifi["standard"]),
                    tracked_row("wifi", "wifi.security_mode", "Security", wifi["security_mode"]),
                    tracked_row("wifi", "wifi.connected_clients", "Clients", wifi["connected_clients"]),
                    tracked_row("wifi", "wifi.password_present", "Password", "Set" if wifi["password_present"] else None),
                ],
            },
            {
                "key": "clients",
                "title": "ACS Clients",
                "rows": [
                    tracked_row("clients", "clients.ethernet_ports_total", "Ethernet Ports", len(ethernet_ports)),
                    tracked_row("clients", "clients.ethernet_ports_active", "Active Ethernet Ports", _count_link_up_ports(ethernet_ports)),
                    tracked_row("clients", "clients.lan_hosts_total", "LAN Hosts", len(lan_hosts)),
                    tracked_row("clients", "clients.lan_hosts_active", "Active LAN Hosts", _count_active(lan_hosts)),
                ],
            },
        ]

    def _map_system(self, group: Mapping[str, Any]) -> dict[str, object]:
        return {
            "manufacturer": _text(_first_value(group, "Manufacturer")),
            "model": _text(_first_value(group, "Model", "Model Name")),
            "firmware": _text(_first_value(group, "Firmware", "Software Version")),
            "hardware": _text(_first_value(group, "Hardware", "Hardware Version")),
            "serial": _text(_first_value(group, "Serial", "Serial Number")),
            "uptime": _text(_first_value(group, "Uptime")),
            "cpu_usage": _text(_first_value(group, "CPU Usage", "CPUUsage")),
            "memory_total": _text(_first_value(group, "Memory Total", "MemoryTotal")),
            "memory_free": _text(_first_value(group, "Memory Free", "MemoryFree")),
            "memory_usage": _text(_first_value(group, "Memory Usage", "MemoryUsage")),
            "mac_address": _text(_first_value(group, "MAC Address", "MACAddress")),
        }

    def _map_wan(self, group: Mapping[str, Any]) -> dict[str, object]:
        return {
            "connection_type": _text(_first_value(group, "Connection Type")),
            "wan_ip": _text(_first_value(group, "WAN IP", "ExternalIPAddress")),
            "pppoe_username": _text(_first_value(group, "Username", "PPPoE Username")),
            "status": _text(_first_value(group, "Status", "ConnectionStatus")),
            "uptime": _text(_first_value(group, "Uptime")),
            "dns_servers": _text(_first_value(group, "DNS Servers", "DNSServers")),
            "gateway": _text(_first_value(group, "Gateway", "DefaultGateway")),
            "wan_instance": _text(_first_value(group, "WAN Instance")),
            "wan_service": _text(_first_value(group, "WAN Service")),
            "management_wan_ip": _text(_first_value(group, "Management WAN IP")),
            "management_wan_status": _text(
                _first_value(group, "Management WAN Status")
            ),
        }

    def _map_lan(self, group: Mapping[str, Any]) -> dict[str, object]:
        return {
            "lan_ip": _text(_first_value(group, "LAN IP", "IPAddress")),
            "subnet_mask": _text(_first_value(group, "Subnet Mask")),
            "dhcp_enabled": _bool_value(_first_value(group, "DHCP Enabled")),
            "dhcp_start": _text(_first_value(group, "DHCP Start")),
            "dhcp_end": _text(_first_value(group, "DHCP End")),
            "connected_hosts": _text(_first_value(group, "Connected Hosts")),
        }

    def _map_wifi(self, group: Mapping[str, Any]) -> dict[str, object]:
        password = _first_value(group, "Password", "KeyPassphrase", "PreSharedKey")
        return {
            "enabled": _bool_value(_first_value(group, "Enabled")),
            "ssid": _text(_first_value(group, "SSID")),
            "channel": _text(_first_value(group, "Channel")),
            "standard": _text(_first_value(group, "Standard")),
            "security_mode": _text(_first_value(group, "Security Mode")),
            "connected_clients": _text(_first_value(group, "Connected Clients")),
            "password_present": _has_secret(password),
        }

    def _map_ethernet_ports(
        self, ports: list[dict[str, Any]]
    ) -> list[dict[str, object]]:
        normalized_ports: list[dict[str, object]] = []
        for index, row in enumerate(ports, start=1):
            port = _as_mapping(row)
            mapped = dict(port)
            mapped.update(
                {
                    "port": _first_value(port, "port", "index", "Port") or index,
                    "admin_enabled": _bool_value(
                        _first_value(port, "admin_enabled", "Enable", "Enabled")
                    ),
                    "link_status": _text(
                        _first_value(port, "link_status", "Status", "status")
                    ),
                    "speed_mbps": _first_value(
                        port, "speed_mbps", "MaxBitRate", "speed"
                    ),
                    "duplex": _text(_first_value(port, "duplex", "DuplexMode")),
                    "mac_address": _text(
                        _first_value(port, "mac_address", "MACAddress", "mac")
                    ),
                    "bytes_sent": _first_value(port, "bytes_sent", "BytesSent"),
                    "bytes_received": _first_value(
                        port, "bytes_received", "BytesReceived"
                    ),
                }
            )
            normalized_ports.append(mapped)
        return normalized_ports

    def _map_lan_hosts(self, hosts: list[dict[str, Any]]) -> list[dict[str, object]]:
        normalized_hosts: list[dict[str, object]] = []
        for row in hosts:
            host = _as_mapping(row)
            active = _bool_value(_first_value(host, "active", "Active"))
            host_name = _text(_first_value(host, "host_name", "hostname", "HostName"))
            ip_address = _text(_first_value(host, "ip_address", "IPAddress"))
            mac_address = _text(_first_value(host, "mac_address", "MACAddress"))
            interface_type = _text(
                _first_value(host, "interface_type", "interface", "InterfaceType")
            )
            mapped = dict(host)
            mapped.update(
                {
                    "host_name": host_name or "",
                    "ip_address": ip_address or "",
                    "mac_address": mac_address or "",
                    "interface_type": interface_type or "",
                    "active": active,
                    "host_name_display": host_name or "-",
                    "ip_address_display": ip_address or "-",
                    "mac_address_display": mac_address or "-",
                    "interface_type_display": interface_type or "-",
                    "active_display": "Active" if active is True else "Inactive",
                }
            )
            normalized_hosts.append(mapped)
        return normalized_hosts

    def refresh_observed_summary_for_ont(self, db: Session, *, ont_id: str) -> object:
        """Refresh/persist ACS observed runtime through the adapter boundary."""
        from app.services.acs_client import create_acs_state_reader

        return create_acs_state_reader().get_device_summary(
            db,
            ont_id,
            persist_observed_runtime=True,
        )


acs_service_intent_adapter = AcsServiceIntentAdapter()
adapter_registry.register(acs_service_intent_adapter)
