"""TR-069 Parameter Adapter - Type-aware parameter handling.

Provides parameter metadata, type inference, and value coercion for TR-069
parameters. Extends tr069_paths.py with type information and validation.

For new code, use:
    from app.services.network.tr069_parameter_adapter import (
        get_parameter_info,
        coerce_value,
        infer_cwmp_type,
    )

    info = get_parameter_info("wan.pppoe_username")
    print(info.cwmp_type, info.access, info.description)

    typed_value = coerce_value("wifi.enabled", "true")
    # Returns: True (bool)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CwmpType(str, Enum):
    """CWMP/TR-069 data types (xsd types)."""

    string = "xsd:string"
    boolean = "xsd:boolean"
    unsigned_int = "xsd:unsignedInt"
    int_ = "xsd:int"
    date_time = "xsd:dateTime"
    base64 = "xsd:base64"
    hex_binary = "xsd:hexBinary"

    def __str__(self) -> str:
        return self.value


class ParameterAccess(str, Enum):
    """Parameter access type."""

    read_only = "R"
    read_write = "RW"
    write_only = "W"

    @property
    def readable(self) -> bool:
        return self in (ParameterAccess.read_only, ParameterAccess.read_write)

    @property
    def writable(self) -> bool:
        return self in (ParameterAccess.read_write, ParameterAccess.write_only)


class ParameterCategory(str, Enum):
    """Parameter category for grouping."""

    system = "system"
    wan = "wan"
    lan = "lan"
    wifi = "wifi"
    ethernet = "ethernet"
    management = "mgmt"
    diagnostics = "diag"
    optical = "optical"
    security = "security"


# ---------------------------------------------------------------------------
# Parameter Info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterInfo:
    """Metadata for a TR-069 parameter."""

    canonical_name: str
    cwmp_type: CwmpType = CwmpType.string
    access: ParameterAccess = ParameterAccess.read_write
    category: ParameterCategory = ParameterCategory.system
    description: str = ""
    unit: str | None = None
    min_value: int | float | None = None
    max_value: int | float | None = None
    enum_values: tuple[str, ...] | None = None
    sensitive: bool = False  # Password, key, etc.
    indexed: bool = False  # Uses {i} instance index

    @property
    def is_boolean(self) -> bool:
        return self.cwmp_type == CwmpType.boolean

    @property
    def is_numeric(self) -> bool:
        return self.cwmp_type in (CwmpType.unsigned_int, CwmpType.int_)

    @property
    def is_sensitive(self) -> bool:
        return self.sensitive

    def validate(self, value: Any) -> tuple[bool, str | None]:
        """Validate a value against this parameter's constraints.

        Returns:
            (is_valid, error_message)
        """
        if value is None:
            return True, None

        # Numeric range validation
        if self.is_numeric:
            try:
                num_value = int(value)
                if self.min_value is not None and num_value < self.min_value:
                    return False, f"Value {num_value} below minimum {self.min_value}"
                if self.max_value is not None and num_value > self.max_value:
                    return False, f"Value {num_value} above maximum {self.max_value}"
            except (ValueError, TypeError):
                return False, f"Expected numeric value, got {type(value).__name__}"

        # Enum validation
        if self.enum_values:
            str_value = str(value).strip()
            if str_value not in self.enum_values:
                return False, f"Value '{str_value}' not in allowed values: {self.enum_values}"

        return True, None


# ---------------------------------------------------------------------------
# Parameter Registry
# ---------------------------------------------------------------------------


# Parameter definitions with full metadata
_PARAMETER_REGISTRY: dict[str, ParameterInfo] = {
    # ── System ──────────────────────────────────────────────────────────
    "system.manufacturer": ParameterInfo(
        canonical_name="system.manufacturer",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Device manufacturer name",
    ),
    "system.model_name": ParameterInfo(
        canonical_name="system.model_name",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Device model name",
    ),
    "system.serial_number": ParameterInfo(
        canonical_name="system.serial_number",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Device serial number",
    ),
    "system.software_version": ParameterInfo(
        canonical_name="system.software_version",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Firmware/software version",
    ),
    "system.hardware_version": ParameterInfo(
        canonical_name="system.hardware_version",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Hardware version",
    ),
    "system.uptime": ParameterInfo(
        canonical_name="system.uptime",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Device uptime",
        unit="seconds",
    ),
    "system.cpu_usage": ParameterInfo(
        canonical_name="system.cpu_usage",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="CPU usage percentage",
        unit="percent",
        min_value=0,
        max_value=100,
    ),
    "system.memory_total": ParameterInfo(
        canonical_name="system.memory_total",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Total memory",
        unit="bytes",
    ),
    "system.memory_free": ParameterInfo(
        canonical_name="system.memory_free",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Free memory",
        unit="bytes",
    ),
    "system.mac_address": ParameterInfo(
        canonical_name="system.mac_address",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.system,
        description="Device MAC address",
    ),
    # ── WAN ─────────────────────────────────────────────────────────────
    "wan.connection_type": ParameterInfo(
        canonical_name="wan.connection_type",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="WAN connection type",
        indexed=True,
    ),
    "wan.ip_address": ParameterInfo(
        canonical_name="wan.ip_address",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="WAN IP address",
        indexed=True,
    ),
    "wan.subnet_mask": ParameterInfo(
        canonical_name="wan.subnet_mask",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="WAN subnet mask",
        indexed=True,
    ),
    "wan.pppoe_username": ParameterInfo(
        canonical_name="wan.pppoe_username",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="PPPoE username",
        indexed=True,
    ),
    "wan.pppoe_password": ParameterInfo(
        canonical_name="wan.pppoe_password",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="PPPoE password",
        sensitive=True,
        indexed=True,
    ),
    "wan.status": ParameterInfo(
        canonical_name="wan.status",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="WAN connection status",
        enum_values=("Up", "Down", "Connecting", "Disconnecting", "Error"),
        indexed=True,
    ),
    "wan.uptime": ParameterInfo(
        canonical_name="wan.uptime",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="WAN connection uptime",
        unit="seconds",
        indexed=True,
    ),
    "wan.dns_servers": ParameterInfo(
        canonical_name="wan.dns_servers",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="DNS server addresses",
    ),
    "wan.gateway": ParameterInfo(
        canonical_name="wan.gateway",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="Default gateway IP",
    ),
    "wan.dhcp_ip": ParameterInfo(
        canonical_name="wan.dhcp_ip",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wan,
        description="DHCP-assigned IP address",
        indexed=True,
    ),
    # ── WAN IPv6 ────────────────────────────────────────────────────────
    "wan.ipv6_enable": ParameterInfo(
        canonical_name="wan.ipv6_enable",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="IPv6 enabled on WAN interface",
        indexed=True,
    ),
    "wan.dhcpv6_enable": ParameterInfo(
        canonical_name="wan.dhcpv6_enable",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="DHCPv6 client enabled",
        indexed=True,
    ),
    "wan.dhcpv6_request_addresses": ParameterInfo(
        canonical_name="wan.dhcpv6_request_addresses",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="Request IPv6 addresses via DHCPv6",
        indexed=True,
    ),
    "wan.dhcpv6_request_prefixes": ParameterInfo(
        canonical_name="wan.dhcpv6_request_prefixes",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="Request IPv6 prefix delegation",
        indexed=True,
    ),
    "wan.router_advertisement_enable": ParameterInfo(
        canonical_name="wan.router_advertisement_enable",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wan,
        description="Router advertisement enabled",
        indexed=True,
    ),
    # ── LAN ─────────────────────────────────────────────────────────────
    "lan.ip_address": ParameterInfo(
        canonical_name="lan.ip_address",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.lan,
        description="LAN IP address",
    ),
    "lan.subnet_mask": ParameterInfo(
        canonical_name="lan.subnet_mask",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.lan,
        description="LAN subnet mask",
    ),
    "lan.dhcp_enabled": ParameterInfo(
        canonical_name="lan.dhcp_enabled",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.lan,
        description="DHCP server enabled",
    ),
    "lan.dhcp_min_address": ParameterInfo(
        canonical_name="lan.dhcp_min_address",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.lan,
        description="DHCP pool start address",
    ),
    "lan.dhcp_max_address": ParameterInfo(
        canonical_name="lan.dhcp_max_address",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.lan,
        description="DHCP pool end address",
    ),
    "lan.host_count": ParameterInfo(
        canonical_name="lan.host_count",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.lan,
        description="Number of connected LAN hosts",
    ),
    # ── WiFi ────────────────────────────────────────────────────────────
    "wifi.enabled": ParameterInfo(
        canonical_name="wifi.enabled",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi radio enabled",
        indexed=True,
    ),
    "wifi.ssid": ParameterInfo(
        canonical_name="wifi.ssid",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi network name (SSID)",
        indexed=True,
    ),
    "wifi.channel": ParameterInfo(
        canonical_name="wifi.channel",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi channel",
        min_value=0,
        max_value=200,
        indexed=True,
    ),
    "wifi.standard": ParameterInfo(
        canonical_name="wifi.standard",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi standard (802.11a/b/g/n/ac/ax)",
        indexed=True,
    ),
    "wifi.security_mode": ParameterInfo(
        canonical_name="wifi.security_mode",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi security mode",
        enum_values=(
            "None", "WEP-64", "WEP-128", "WPA-Personal", "WPA2-Personal",
            "WPA-WPA2-Personal", "WPA3-Personal", "WPA-Enterprise",
            "WPA2-Enterprise", "WPA3-Enterprise",
        ),
        indexed=True,
    ),
    "wifi.client_count": ParameterInfo(
        canonical_name="wifi.client_count",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_only,
        category=ParameterCategory.wifi,
        description="Number of connected WiFi clients",
        indexed=True,
    ),
    "wifi.psk": ParameterInfo(
        canonical_name="wifi.psk",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.wifi,
        description="WiFi pre-shared key (password)",
        sensitive=True,
        indexed=True,
    ),
    # ── Ethernet ────────────────────────────────────────────────────────
    "ethernet.port_enable": ParameterInfo(
        canonical_name="ethernet.port_enable",
        cwmp_type=CwmpType.boolean,
        access=ParameterAccess.read_write,
        category=ParameterCategory.ethernet,
        description="Ethernet port enabled",
        indexed=True,
    ),
    "ethernet.port_status": ParameterInfo(
        canonical_name="ethernet.port_status",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.ethernet,
        description="Ethernet port status",
        enum_values=("Up", "Down", "Unknown", "Dormant", "NotPresent", "LowerLayerDown"),
        indexed=True,
    ),
    "ethernet.port_max_bitrate": ParameterInfo(
        canonical_name="ethernet.port_max_bitrate",
        cwmp_type=CwmpType.int_,
        access=ParameterAccess.read_only,
        category=ParameterCategory.ethernet,
        description="Ethernet port max bitrate",
        unit="Mbps",
        indexed=True,
    ),
    "ethernet.port_duplex": ParameterInfo(
        canonical_name="ethernet.port_duplex",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.ethernet,
        description="Ethernet port duplex mode",
        enum_values=("Full", "Half", "Auto"),
        indexed=True,
    ),
    "ethernet.port_mac": ParameterInfo(
        canonical_name="ethernet.port_mac",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.ethernet,
        description="Ethernet port MAC address",
        indexed=True,
    ),
    # ── Management ──────────────────────────────────────────────────────
    "mgmt.conn_request_url": ParameterInfo(
        canonical_name="mgmt.conn_request_url",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_only,
        category=ParameterCategory.management,
        description="Connection request URL",
    ),
    "mgmt.conn_request_username": ParameterInfo(
        canonical_name="mgmt.conn_request_username",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.management,
        description="Connection request username",
    ),
    "mgmt.conn_request_password": ParameterInfo(
        canonical_name="mgmt.conn_request_password",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.management,
        description="Connection request password",
        sensitive=True,
    ),
    "mgmt.periodic_inform_interval": ParameterInfo(
        canonical_name="mgmt.periodic_inform_interval",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_write,
        category=ParameterCategory.management,
        description="ACS inform interval",
        unit="seconds",
        min_value=60,
        max_value=86400,
    ),
    # ── Diagnostics ─────────────────────────────────────────────────────
    "diag.ping.host": ParameterInfo(
        canonical_name="diag.ping.host",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.diagnostics,
        description="Ping diagnostic target host",
    ),
    "diag.ping.repetitions": ParameterInfo(
        canonical_name="diag.ping.repetitions",
        cwmp_type=CwmpType.unsigned_int,
        access=ParameterAccess.read_write,
        category=ParameterCategory.diagnostics,
        description="Ping repetition count",
        min_value=1,
        max_value=100,
    ),
    "diag.ping.state": ParameterInfo(
        canonical_name="diag.ping.state",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.diagnostics,
        description="Ping diagnostic state",
        enum_values=("None", "Requested", "Canceled", "Complete", "Error_CannotResolveHostName"),
    ),
    "diag.traceroute.host": ParameterInfo(
        canonical_name="diag.traceroute.host",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.diagnostics,
        description="Traceroute diagnostic target host",
    ),
    "diag.traceroute.state": ParameterInfo(
        canonical_name="diag.traceroute.state",
        cwmp_type=CwmpType.string,
        access=ParameterAccess.read_write,
        category=ParameterCategory.diagnostics,
        description="Traceroute diagnostic state",
    ),
    # ── Optical ─────────────────────────────────────────────────────────
    "optical.signal_level": ParameterInfo(
        canonical_name="optical.signal_level",
        cwmp_type=CwmpType.int_,
        access=ParameterAccess.read_only,
        category=ParameterCategory.optical,
        description="Optical receive signal level",
        unit="dBm",
        indexed=True,
    ),
    "optical.lower_threshold": ParameterInfo(
        canonical_name="optical.lower_threshold",
        cwmp_type=CwmpType.int_,
        access=ParameterAccess.read_only,
        category=ParameterCategory.optical,
        description="Optical lower threshold",
        unit="dBm",
        indexed=True,
    ),
    "optical.upper_threshold": ParameterInfo(
        canonical_name="optical.upper_threshold",
        cwmp_type=CwmpType.int_,
        access=ParameterAccess.read_only,
        category=ParameterCategory.optical,
        description="Optical upper threshold",
        unit="dBm",
        indexed=True,
    ),
    "optical.transmit_level": ParameterInfo(
        canonical_name="optical.transmit_level",
        cwmp_type=CwmpType.int_,
        access=ParameterAccess.read_only,
        category=ParameterCategory.optical,
        description="Optical transmit level",
        unit="dBm",
        indexed=True,
    ),
}


# ---------------------------------------------------------------------------
# Type Inference
# ---------------------------------------------------------------------------

# Path suffixes that indicate boolean parameters
_BOOLEAN_SUFFIXES = frozenset({
    "enable", "enabled", "active", "dhcpenable", "dhcpenabled",
    "ipv4enable", "ipv6enable", "igmpenable", "beaconsecurityenable",
})

# Boolean value strings
_BOOLEAN_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
_BOOLEAN_FALSE_VALUES = frozenset({"false", "0", "no", "off"})
_BOOLEAN_VALUE_STRINGS = _BOOLEAN_TRUE_VALUES | _BOOLEAN_FALSE_VALUES


def infer_cwmp_type(path: str, value: Any) -> CwmpType:
    """Infer CWMP type from path and value.

    Args:
        path: Full CWMP path (e.g., "Device.WiFi.SSID.1.Enable")
        value: Value to analyze

    Returns:
        Inferred CwmpType
    """
    # Python bool -> xsd:boolean
    if isinstance(value, bool):
        return CwmpType.boolean

    # Python int -> check if it's a string masquerading as bool
    if isinstance(value, int) and not isinstance(value, bool):
        return CwmpType.unsigned_int if value >= 0 else CwmpType.int_

    # String analysis
    str_value = str(value).strip().lower()

    # Check for boolean-like values with boolean-like paths
    if str_value in _BOOLEAN_VALUE_STRINGS:
        leaf = path.rsplit(".", 1)[-1].lower()
        if leaf in _BOOLEAN_SUFFIXES:
            return CwmpType.boolean
        if leaf.endswith("enable") or leaf.endswith("enabled"):
            return CwmpType.boolean

    # Check for numeric strings
    if str_value.lstrip("-").isdigit():
        try:
            int_val = int(str_value)
            return CwmpType.unsigned_int if int_val >= 0 else CwmpType.int_
        except ValueError:
            pass

    return CwmpType.string


def infer_cwmp_type_string(path: str, value: Any) -> str:
    """Infer CWMP type and return as xsd string.

    This is the function used by GenieACS for setParameterValues.

    Args:
        path: Full CWMP path
        value: Value to analyze

    Returns:
        xsd type string (e.g., "xsd:boolean")
    """
    return str(infer_cwmp_type(path, value))


# ---------------------------------------------------------------------------
# Value Coercion
# ---------------------------------------------------------------------------


def coerce_value(canonical_name: str, value: Any) -> Any:
    """Coerce a value to the correct Python type for a parameter.

    Args:
        canonical_name: Canonical parameter name
        value: Raw value (usually string from TR-069)

    Returns:
        Coerced value in appropriate Python type
    """
    if value is None:
        return None

    info = _PARAMETER_REGISTRY.get(canonical_name)
    if info is None:
        # Unknown parameter - return as-is
        return value

    return _coerce_by_type(value, info.cwmp_type)


def coerce_value_by_path(path: str, value: Any) -> Any:
    """Coerce a value based on its CWMP path.

    Useful when you have a path but not a canonical name.

    Args:
        path: Full CWMP path
        value: Raw value

    Returns:
        Coerced value
    """
    if value is None:
        return None

    inferred_type = infer_cwmp_type(path, value)
    return _coerce_by_type(value, inferred_type)


def _coerce_by_type(value: Any, cwmp_type: CwmpType) -> Any:
    """Coerce value to match CWMP type."""
    if value is None:
        return None

    if cwmp_type == CwmpType.boolean:
        if isinstance(value, bool):
            return value
        str_val = str(value).strip().lower()
        return str_val in _BOOLEAN_TRUE_VALUES

    if cwmp_type in (CwmpType.unsigned_int, CwmpType.int_):
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return 0

    # Default: string
    return str(value) if value is not None else ""


# ---------------------------------------------------------------------------
# Registry Access
# ---------------------------------------------------------------------------


def get_parameter_info(canonical_name: str) -> ParameterInfo | None:
    """Get parameter metadata by canonical name.

    Args:
        canonical_name: Canonical parameter name (e.g., "wifi.ssid")

    Returns:
        ParameterInfo or None if not found
    """
    return _PARAMETER_REGISTRY.get(canonical_name)


def get_parameter_info_required(canonical_name: str) -> ParameterInfo:
    """Get parameter metadata, raising if not found.

    Args:
        canonical_name: Canonical parameter name

    Returns:
        ParameterInfo

    Raises:
        KeyError: If parameter not found
    """
    info = _PARAMETER_REGISTRY.get(canonical_name)
    if info is None:
        raise KeyError(f"Unknown parameter: {canonical_name}")
    return info


def list_parameters(
    *,
    category: ParameterCategory | None = None,
    access: ParameterAccess | None = None,
    writable_only: bool = False,
    readable_only: bool = False,
) -> list[ParameterInfo]:
    """List parameters matching filter criteria.

    Args:
        category: Filter by category
        access: Filter by access type
        writable_only: Only include writable parameters
        readable_only: Only include readable parameters

    Returns:
        List of matching ParameterInfo objects
    """
    results = []

    for info in _PARAMETER_REGISTRY.values():
        if category and info.category != category:
            continue
        if access and info.access != access:
            continue
        if writable_only and not info.access.writable:
            continue
        if readable_only and not info.access.readable:
            continue
        results.append(info)

    return sorted(results, key=lambda p: p.canonical_name)


def list_canonical_names(
    *,
    category: ParameterCategory | None = None,
) -> list[str]:
    """List canonical parameter names.

    Args:
        category: Optional category filter

    Returns:
        Sorted list of canonical names
    """
    if category:
        return sorted(
            name for name, info in _PARAMETER_REGISTRY.items()
            if info.category == category
        )
    return sorted(_PARAMETER_REGISTRY.keys())


def list_categories() -> list[ParameterCategory]:
    """List all parameter categories."""
    return list(ParameterCategory)


# ---------------------------------------------------------------------------
# Batch Operations
# ---------------------------------------------------------------------------


@dataclass
class TypedParameterValue:
    """A parameter value with type information."""

    canonical_name: str
    path: str
    value: Any
    cwmp_type: CwmpType
    info: ParameterInfo | None = None

    @property
    def xsd_type(self) -> str:
        """Get xsd type string for GenieACS."""
        return str(self.cwmp_type)


def prepare_parameter_values(
    param_values: dict[str, Any],
    data_model_root: str = "Device",
    *,
    vendor: str | None = None,
    model: str | None = None,
    instance_index: int = 1,
    db: Any | None = None,
) -> list[TypedParameterValue]:
    """Prepare parameter values with resolved paths and types.

    Args:
        param_values: {canonical_name: value} dict
        data_model_root: "Device" or "InternetGatewayDevice"
        vendor: Device vendor for overrides
        model: Device model for overrides
        instance_index: Instance index for indexed parameters
        db: Database session for vendor overrides

    Returns:
        List of TypedParameterValue ready for setParameterValues
    """
    from app.services.network.tr069_paths import tr069_path_resolver

    results = []

    for canonical_name, value in param_values.items():
        # Get parameter info
        info = _PARAMETER_REGISTRY.get(canonical_name)

        # Resolve path
        try:
            path = tr069_path_resolver.resolve(
                data_model_root,
                canonical_name,
                db=db,
                vendor=vendor,
                model=model,
                instance_index=instance_index,
            )
        except Exception as exc:
            logger.warning("Failed to resolve path for %s: %s", canonical_name, exc)
            continue

        # Determine type
        if info:
            cwmp_type = info.cwmp_type
        else:
            cwmp_type = infer_cwmp_type(path, value)

        # Coerce value
        coerced = _coerce_by_type(value, cwmp_type)

        results.append(TypedParameterValue(
            canonical_name=canonical_name,
            path=path,
            value=coerced,
            cwmp_type=cwmp_type,
            info=info,
        ))

    return results


def to_genieacs_params(typed_values: list[TypedParameterValue]) -> list[list]:
    """Convert typed values to GenieACS setParameterValues format.

    Args:
        typed_values: List of TypedParameterValue

    Returns:
        List of [path, value, xsd_type] for GenieACS
    """
    return [
        [tv.path, tv.value, tv.xsd_type]
        for tv in typed_values
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of parameter validation."""

    is_valid: bool
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return len(self.errors)


def validate_parameters(param_values: dict[str, Any]) -> ValidationResult:
    """Validate parameter values against their constraints.

    Args:
        param_values: {canonical_name: value} dict

    Returns:
        ValidationResult with any errors
    """
    errors = {}

    for canonical_name, value in param_values.items():
        info = _PARAMETER_REGISTRY.get(canonical_name)
        if info is None:
            continue

        # Check writability
        if not info.access.writable:
            errors[canonical_name] = "Parameter is read-only"
            continue

        # Validate value
        is_valid, error_msg = info.validate(value)
        if not is_valid:
            errors[canonical_name] = error_msg or "Invalid value"

    return ValidationResult(
        is_valid=len(errors) == 0,
        errors=errors,
    )
