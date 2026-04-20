"""Adapter for ACS/TR-069 observed service intent data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

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


class AcsServiceIntentAdapter:
    """Normalize ACS observed state into service-intent-shaped UI data."""

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

        system = self._map_system(system_group)
        wan = self._map_wan(wan_group)
        lan = self._map_lan(lan_group)
        wifi = self._map_wifi(wireless_group)
        ethernet_ports = self._map_ethernet_ports(
            list(_summary_attr(summary, "ethernet_ports", []) or [])
        )
        lan_hosts = self._map_lan_hosts(
            list(_summary_attr(summary, "lan_hosts", []) or [])
        )

        sections = [
            {
                "title": "ACS System",
                "rows": [
                    _row("Manufacturer", system["manufacturer"]),
                    _row("Model", system["model"]),
                    _row("Firmware", system["firmware"]),
                    _row("Serial", system["serial"]),
                    _row("Uptime", system["uptime"]),
                ],
            },
            {
                "title": "ACS WAN",
                "rows": [
                    _row("Connection", wan["connection_type"]),
                    _row("Status", wan["status"]),
                    _row("WAN IP", wan["wan_ip"]),
                    _row("PPPoE User", wan["pppoe_username"]),
                    _row("Gateway", wan["gateway"]),
                    _row("DNS", wan["dns_servers"]),
                ],
            },
            {
                "title": "ACS LAN",
                "rows": [
                    _row("LAN IP", lan["lan_ip"]),
                    _row("Subnet", lan["subnet_mask"]),
                    _row("DHCP", lan["dhcp_enabled"]),
                    _row("DHCP Start", lan["dhcp_start"]),
                    _row("DHCP End", lan["dhcp_end"]),
                    _row("Hosts", lan["connected_hosts"]),
                ],
            },
            {
                "title": "ACS WiFi",
                "rows": [
                    _row("Enabled", wifi["enabled"]),
                    _row("SSID", wifi["ssid"]),
                    _row("Channel", wifi["channel"]),
                    _row("Standard", wifi["standard"]),
                    _row("Security", wifi["security_mode"]),
                    _row("Clients", wifi["connected_clients"]),
                    _row("Password", "Set" if wifi["password_present"] else None),
                ],
            },
            {
                "title": "ACS Clients",
                "rows": [
                    _row("Ethernet Ports", len(ethernet_ports)),
                    _row("LAN Hosts", len(lan_hosts)),
                ],
            },
        ]

        observed = {
            "system": system,
            "wan": wan,
            "lan": lan,
            "wifi": wifi,
            "ethernet_ports": ethernet_ports,
            "lan_hosts": lan_hosts,
        }
        missing_count = _missing_count(system, wan, lan, wifi)

        return {
            "available": available,
            "source": source,
            "fetched_at": fetched_at,
            "error": _text(error),
            "observed": observed,
            "sections": sections if available else [],
            "missing_count": missing_count,
            "is_complete": available and missing_count == 0,
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
            "missing_count": 0,
            "is_complete": False,
        }

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
