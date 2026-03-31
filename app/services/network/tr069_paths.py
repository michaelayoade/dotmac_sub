"""TR-069 parameter path resolution.

Provides deterministic path resolution for TR-181 (Device) and TR-098
(InternetGatewayDevice) data models.  Each canonical parameter name maps
to exactly ONE correct CWMP path per standard.  Vendor-specific overrides
are sourced from the ``Tr069ParameterMap`` database table.

Usage::

    from app.services.network.tr069_paths import tr069_path_resolver

    path = tr069_path_resolver.resolve("Device", "wan.pppoe_username")
    # => "Device.PPP.Interface.1.Username"

    path = tr069_path_resolver.resolve(
        "InternetGatewayDevice", "wifi.ssid", instance_index=2,
    )
    # => "InternetGatewayDevice.LANDevice.1.WLANConfiguration.2.SSID"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TR069_ROOT_DEVICE = "Device"
TR069_ROOT_IGD = "InternetGatewayDevice"

_VALID_ROOTS = frozenset({TR069_ROOT_DEVICE, TR069_ROOT_IGD})


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class Tr069PathError(Exception):
    """Raised when a canonical name cannot be resolved to a TR-069 path."""


# ---------------------------------------------------------------------------
# Standard path templates — TR-181 (Device)
#
# Paths are *suffixes* (root prefix is prepended by the resolver).
# ``{i}`` is replaced with the instance index at resolve time.
# ---------------------------------------------------------------------------

_TR181_PATHS: dict[str, str] = {
    # ── System ──────────────────────────────────────────────────────────
    "system.manufacturer": "DeviceInfo.Manufacturer",
    "system.model_name": "DeviceInfo.ModelName",
    "system.serial_number": "DeviceInfo.SerialNumber",
    "system.software_version": "DeviceInfo.SoftwareVersion",
    "system.hardware_version": "DeviceInfo.HardwareVersion",
    "system.uptime": "DeviceInfo.UpTime",
    "system.cpu_usage": "DeviceInfo.ProcessStatus.CPUUsage",
    "system.memory_total": "DeviceInfo.MemoryStatus.Total",
    "system.memory_free": "DeviceInfo.MemoryStatus.Free",
    "system.mac_address": "Ethernet.Interface.1.MACAddress",
    # ── WAN ─────────────────────────────────────────────────────────────
    "wan.connection_type": "PPP.Interface.{i}.ConnectionStatus",
    "wan.ip_address": "IP.Interface.{i}.IPv4Address.1.IPAddress",
    "wan.subnet_mask": "IP.Interface.{i}.IPv4Address.1.SubnetMask",
    "wan.pppoe_username": "PPP.Interface.{i}.Username",
    "wan.pppoe_password": "PPP.Interface.{i}.Password",
    "wan.status": "IP.Interface.{i}.Status",
    "wan.uptime": "PPP.Interface.{i}.Uptime",
    "wan.dns_servers": "DNS.Client.Server.1.DNSServer",
    "wan.gateway": "Routing.Router.1.IPv4Forwarding.1.GatewayIPAddress",
    "wan.dhcp_ip": "DHCPv4.Client.{i}.IPAddress",
    # ── WAN IPv6 ────────────────────────────────────────────────────────
    "wan.ipv6_enable": "IP.Interface.{i}.IPv6Enable",
    "wan.dhcpv6_enable": "DHCPv6.Client.{i}.Enable",
    "wan.dhcpv6_request_addresses": "DHCPv6.Client.{i}.RequestAddresses",
    "wan.dhcpv6_request_prefixes": "DHCPv6.Client.{i}.RequestPrefixes",
    "wan.router_advertisement_enable": "RouterAdvertisement.InterfaceSettings.{i}.Enable",
    # ── LAN ─────────────────────────────────────────────────────────────
    "lan.ip_address": "IP.Interface.2.IPv4Address.1.IPAddress",
    "lan.subnet_mask": "IP.Interface.2.IPv4Address.1.SubnetMask",
    "lan.dhcp_enabled": "DHCPv4.Server.Enable",
    "lan.dhcp_min_address": "DHCPv4.Server.Pool.1.MinAddress",
    "lan.dhcp_max_address": "DHCPv4.Server.Pool.1.MaxAddress",
    "lan.host_count": "Hosts.HostNumberOfEntries",
    # ── WiFi ────────────────────────────────────────────────────────────
    "wifi.enabled": "WiFi.SSID.{i}.Enable",
    "wifi.ssid": "WiFi.SSID.{i}.SSID",
    "wifi.channel": "WiFi.Radio.{i}.Channel",
    "wifi.standard": "WiFi.Radio.{i}.OperatingStandards",
    "wifi.security_mode": "WiFi.AccessPoint.{i}.Security.ModeEnabled",
    "wifi.client_count": "WiFi.AccessPoint.{i}.AssociatedDeviceNumberOfEntries",
    "wifi.psk": "WiFi.AccessPoint.{i}.Security.KeyPassphrase",
    # ── Ethernet ────────────────────────────────────────────────────────
    "ethernet.port_enable": "Ethernet.Interface.{i}.Enable",
    "ethernet.port_status": "Ethernet.Interface.{i}.Status",
    "ethernet.port_max_bitrate": "Ethernet.Interface.{i}.MaxBitRate",
    "ethernet.port_duplex": "Ethernet.Interface.{i}.DuplexMode",
    "ethernet.port_mac": "Ethernet.Interface.{i}.MACAddress",
    # ── Management ──────────────────────────────────────────────────────
    "mgmt.conn_request_url": "ManagementServer.ConnectionRequestURL",
    "mgmt.conn_request_username": "ManagementServer.ConnectionRequestUsername",
    "mgmt.conn_request_password": "ManagementServer.ConnectionRequestPassword",
    "mgmt.periodic_inform_interval": "ManagementServer.PeriodicInformInterval",
    # ── Diagnostics ─────────────────────────────────────────────────────
    "diag.ping.host": "IP.Diagnostics.IPPing.Host",
    "diag.ping.repetitions": "IP.Diagnostics.IPPing.NumberOfRepetitions",
    "diag.ping.state": "IP.Diagnostics.IPPing.DiagnosticsState",
    "diag.traceroute.host": "IP.Diagnostics.TraceRoute.Host",
    "diag.traceroute.state": "IP.Diagnostics.TraceRoute.DiagnosticsState",
    # ── Optical ─────────────────────────────────────────────────────────
    "optical.signal_level": "Optical.Interface.{i}.OpticalSignalLevel",
    "optical.lower_threshold": "Optical.Interface.{i}.LowerOpticalThreshold",
    "optical.upper_threshold": "Optical.Interface.{i}.UpperOpticalThreshold",
    "optical.transmit_level": "Optical.Interface.{i}.TransmitOpticalLevel",
}


# ---------------------------------------------------------------------------
# Standard path templates — TR-098 (InternetGatewayDevice)
# ---------------------------------------------------------------------------

_TR098_PATHS: dict[str, str] = {
    # ── System ──────────────────────────────────────────────────────────
    "system.manufacturer": "DeviceInfo.Manufacturer",
    "system.model_name": "DeviceInfo.ModelName",
    "system.serial_number": "DeviceInfo.SerialNumber",
    "system.software_version": "DeviceInfo.SoftwareVersion",
    "system.hardware_version": "DeviceInfo.HardwareVersion",
    "system.uptime": "DeviceInfo.UpTime",
    "system.cpu_usage": "DeviceInfo.ProcessStatus.CPUUsage",
    "system.memory_total": "DeviceInfo.MemoryStatus.Total",
    "system.memory_free": "DeviceInfo.MemoryStatus.Free",
    "system.mac_address": "LANDevice.1.LANEthernetInterfaceConfig.1.MACAddress",
    # ── WAN ─────────────────────────────────────────────────────────────
    "wan.connection_type": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.ConnectionType",
    "wan.ip_address": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.ExternalIPAddress",
    "wan.ip_address_dhcp": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ExternalIPAddress",
    "wan.subnet_mask": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.SubnetMask",
    "wan.pppoe_username": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Username",
    "wan.pppoe_password": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Password",
    "wan.status": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.ConnectionStatus",
    "wan.status_ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ConnectionStatus",
    "wan.uptime": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.Uptime",
    "wan.dns_servers": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.DNSServers",
    "wan.dns_servers_ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DNSServers",
    "wan.gateway": "WANDevice.1.WANConnectionDevice.{i}.WANPPPConnection.1.DefaultGateway",
    "wan.gateway_ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.DefaultGateway",
    "wan.dhcp_ip": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.ExternalIPAddress",
    # ── WAN IPv6 ────────────────────────────────────────────────────────
    "wan.ipv6_enable": "WANDevice.1.WANConnectionDevice.{i}.WANIPConnection.1.X_IPv6Enabled",
    # ── LAN ─────────────────────────────────────────────────────────────
    "lan.ip_address": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
    "lan.subnet_mask": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
    "lan.dhcp_enabled": "LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
    "lan.dhcp_min_address": "LANDevice.1.LANHostConfigManagement.MinAddress",
    "lan.dhcp_max_address": "LANDevice.1.LANHostConfigManagement.MaxAddress",
    "lan.host_count": "LANDevice.1.Hosts.HostNumberOfEntries",
    # ── WiFi ────────────────────────────────────────────────────────────
    "wifi.enabled": "LANDevice.1.WLANConfiguration.{i}.Enable",
    "wifi.ssid": "LANDevice.1.WLANConfiguration.{i}.SSID",
    "wifi.channel": "LANDevice.1.WLANConfiguration.{i}.Channel",
    "wifi.standard": "LANDevice.1.WLANConfiguration.{i}.Standard",
    "wifi.security_mode": "LANDevice.1.WLANConfiguration.{i}.BeaconType",
    "wifi.client_count": "LANDevice.1.WLANConfiguration.{i}.TotalAssociations",
    "wifi.psk": "LANDevice.1.WLANConfiguration.{i}.PreSharedKey.1.PreSharedKey",
    # ── Ethernet ────────────────────────────────────────────────────────
    "ethernet.port_enable": "LANDevice.1.LANEthernetInterfaceConfig.{i}.Enable",
    "ethernet.port_status": "LANDevice.1.LANEthernetInterfaceConfig.{i}.Status",
    "ethernet.port_max_bitrate": "LANDevice.1.LANEthernetInterfaceConfig.{i}.MaxBitRate",
    "ethernet.port_duplex": "LANDevice.1.LANEthernetInterfaceConfig.{i}.DuplexMode",
    "ethernet.port_mac": "LANDevice.1.LANEthernetInterfaceConfig.{i}.MACAddress",
    # ── Management ──────────────────────────────────────────────────────
    "mgmt.conn_request_url": "ManagementServer.ConnectionRequestURL",
    "mgmt.conn_request_username": "ManagementServer.ConnectionRequestUsername",
    "mgmt.conn_request_password": "ManagementServer.ConnectionRequestPassword",
    "mgmt.periodic_inform_interval": "ManagementServer.PeriodicInformInterval",
    # ── Diagnostics ─────────────────────────────────────────────────────
    "diag.ping.host": "IPPingDiagnostics.Host",
    "diag.ping.repetitions": "IPPingDiagnostics.NumberOfRepetitions",
    "diag.ping.state": "IPPingDiagnostics.DiagnosticsState",
    "diag.traceroute.host": "TraceRouteDiagnostics.Host",
    "diag.traceroute.state": "TraceRouteDiagnostics.DiagnosticsState",
    # ── Optical (not standard in TR-098; vendor extension paths) ───────
    "optical.signal_level": "WANDevice.1.X_GponInterafceConfig.RXPower",
    "optical.lower_threshold": "WANDevice.1.X_GponInterafceConfig.LowerRXThreshold",
    "optical.upper_threshold": "WANDevice.1.X_GponInterafceConfig.UpperRXThreshold",
    "optical.transmit_level": "WANDevice.1.X_GponInterafceConfig.TXPower",
}


# ---------------------------------------------------------------------------
# Object base paths — for enumerating numbered objects (ports, hosts)
# ---------------------------------------------------------------------------

_TR181_OBJECT_BASES: dict[str, str] = {
    "ethernet_ports": "Ethernet.Interface.",
    "lan_hosts": "Hosts.Host.",
    "wifi_ssids": "WiFi.SSID.",
}

_TR098_OBJECT_BASES: dict[str, str] = {
    "ethernet_ports": "LANDevice.1.LANEthernetInterfaceConfig.",
    "lan_hosts": "LANDevice.1.Hosts.Host.",
    "wifi_ssids": "LANDevice.1.WLANConfiguration.",
}

_OBJECT_BASES: dict[str, dict[str, str]] = {
    TR069_ROOT_DEVICE: _TR181_OBJECT_BASES,
    TR069_ROOT_IGD: _TR098_OBJECT_BASES,
}

# Object field suffixes (appended after instance number)
ETHERNET_PORT_FIELDS = ("Enable", "Status", "MaxBitRate", "DuplexMode", "MACAddress")
LAN_HOST_FIELDS = ("HostName", "IPAddress", "MACAddress", "InterfaceType", "Active")


# ---------------------------------------------------------------------------
# Backward-compatibility mapping: old PARAM_GROUPS labels → canonical names
# Used during transition to let ont_tr069.py's display code migrate gradually.
# ---------------------------------------------------------------------------

LABEL_TO_CANONICAL: dict[str, str] = {
    "system.Manufacturer": "system.manufacturer",
    "system.Model": "system.model_name",
    "system.Firmware": "system.software_version",
    "system.Hardware": "system.hardware_version",
    "system.Serial": "system.serial_number",
    "system.Uptime": "system.uptime",
    "system.CPU Usage": "system.cpu_usage",
    "system.Memory Total": "system.memory_total",
    "system.Memory Free": "system.memory_free",
    "system.MAC Address": "system.mac_address",
    "wan.Connection Type": "wan.connection_type",
    "wan.WAN IP": "wan.ip_address",
    "wan.Username": "wan.pppoe_username",
    "wan.Status": "wan.status",
    "wan.Uptime": "wan.uptime",
    "wan.DNS Servers": "wan.dns_servers",
    "wan.Gateway": "wan.gateway",
    "lan.LAN IP": "lan.ip_address",
    "lan.Subnet Mask": "lan.subnet_mask",
    "lan.DHCP Enabled": "lan.dhcp_enabled",
    "lan.DHCP Start": "lan.dhcp_min_address",
    "lan.DHCP End": "lan.dhcp_max_address",
    "lan.Connected Hosts": "lan.host_count",
    "wireless.Enabled": "wifi.enabled",
    "wireless.SSID": "wifi.ssid",
    "wireless.Channel": "wifi.channel",
    "wireless.Standard": "wifi.standard",
    "wireless.Security Mode": "wifi.security_mode",
    "wireless.Connected Clients": "wifi.client_count",
    "wireless.Password": "wifi.psk",
}


# ---------------------------------------------------------------------------
# Display group definitions — maps section + label to canonical names
# Used by ont_tr069.py / cpe_tr069.py for the TR-069 tab display.
# ---------------------------------------------------------------------------

DISPLAY_GROUPS: dict[str, dict[str, str]] = {
    "system": {
        "Manufacturer": "system.manufacturer",
        "Model": "system.model_name",
        "Firmware": "system.software_version",
        "Hardware": "system.hardware_version",
        "Serial": "system.serial_number",
        "Uptime": "system.uptime",
        "CPU Usage": "system.cpu_usage",
        "Memory Total": "system.memory_total",
        "Memory Free": "system.memory_free",
        "MAC Address": "system.mac_address",
    },
    "wan": {
        "Connection Type": "wan.connection_type",
        "WAN IP": "wan.ip_address",
        "Username": "wan.pppoe_username",
        "Status": "wan.status",
        "Uptime": "wan.uptime",
        "DNS Servers": "wan.dns_servers",
        "Gateway": "wan.gateway",
    },
    "lan": {
        "LAN IP": "lan.ip_address",
        "Subnet Mask": "lan.subnet_mask",
        "DHCP Enabled": "lan.dhcp_enabled",
        "DHCP Start": "lan.dhcp_min_address",
        "DHCP End": "lan.dhcp_max_address",
        "Connected Hosts": "lan.host_count",
    },
    "wireless": {
        "Enabled": "wifi.enabled",
        "SSID": "wifi.ssid",
        "Channel": "wifi.channel",
        "Standard": "wifi.standard",
        "Security Mode": "wifi.security_mode",
        "Connected Clients": "wifi.client_count",
        "Password": "wifi.psk",
    },
}


# ---------------------------------------------------------------------------
# Running config parameter groups — used by ont_action_device.py
# ---------------------------------------------------------------------------

RUNNING_CONFIG_GROUPS: dict[str, list[str]] = {
    "device_info": [
        "system.manufacturer",
        "system.model_name",
        "system.serial_number",
        "system.software_version",
        "system.hardware_version",
        "system.uptime",
        "system.memory_total",
        "system.memory_free",
    ],
    "wan": [
        "wan.ip_address",
        "wan.subnet_mask",
        "wan.status",
        "wan.dhcp_ip",
    ],
    "optical": [
        "optical.signal_level",
        "optical.lower_threshold",
        "optical.upper_threshold",
        "optical.transmit_level",
    ],
    "wifi": [
        "wifi.ssid",
        "wifi.enabled",
        "wifi.channel",
        "wifi.standard",
    ],
}


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

_STANDARD_PATHS: dict[str, dict[str, str]] = {
    TR069_ROOT_DEVICE: _TR181_PATHS,
    TR069_ROOT_IGD: _TR098_PATHS,
}


class Tr069PathResolver:
    """Resolve canonical parameter names to full TR-069 CWMP paths.

    Resolution order:
    1. Vendor-specific override via ``Tr069ParameterMap`` (if db/vendor/model provided)
    2. Standard path dict for the detected data model root
    3. Raise ``Tr069PathError``
    """

    def _validate_root(self, root: str) -> None:
        if root not in _VALID_ROOTS:
            raise Tr069PathError(
                f"Invalid data model root '{root}'. "
                f"Expected 'Device' or 'InternetGatewayDevice'. "
                f"Trigger a TR-069 inform to detect the data model."
            )

    def _resolve_vendor_override(
        self,
        canonical_name: str,
        *,
        db: Session | None = None,
        vendor: str | None = None,
        model: str | None = None,
    ) -> str | None:
        """Check the vendor capability DB for a device-specific path override."""
        if db is None or not vendor or not model:
            return None
        try:
            from app.services.network.vendor_capabilities import (
                Tr069ParameterMaps,
                VendorCapabilities,
            )

            capability = VendorCapabilities.resolve_capability(
                db, vendor=vendor, model=model
            )
            if capability is None:
                return None
            path = Tr069ParameterMaps.resolve_path(
                db, capability_id=str(capability.id), canonical_name=canonical_name
            )
            if path:
                logger.debug(
                    "Vendor override for %s/%s: %s → %s",
                    vendor,
                    model,
                    canonical_name,
                    path,
                )
            return path
        except Exception:
            logger.debug(
                "Vendor capability lookup failed for %s/%s", vendor, model,
                exc_info=True,
            )
            return None

    def resolve(
        self,
        root: str,
        canonical_name: str,
        *,
        db: Session | None = None,
        vendor: str | None = None,
        model: str | None = None,
        instance_index: int = 1,
    ) -> str:
        """Return the full TR-069 CWMP path for a canonical parameter name.

        Args:
            root: "Device" (TR-181) or "InternetGatewayDevice" (TR-098).
            canonical_name: Dot-delimited name (e.g. "wan.pppoe_username").
            db: Optional DB session for vendor override lookup.
            vendor: Device vendor string (e.g. "Huawei").
            model: Device model string (e.g. "HG8145V5").
            instance_index: Instance number for ``{i}`` substitution (default 1).

        Returns:
            Full CWMP path (e.g. "Device.PPP.Interface.1.Username").

        Raises:
            Tr069PathError: If root is invalid or canonical name is unknown.
        """
        self._validate_root(root)

        # 1. Try vendor-specific override
        override = self._resolve_vendor_override(
            canonical_name, db=db, vendor=vendor, model=model
        )
        if override:
            suffix = override.replace("{i}", str(instance_index))
            # Override may be a full path or a suffix
            if suffix.startswith(root + "."):
                return suffix
            return f"{root}.{suffix}"

        # 2. Standard path lookup
        paths = _STANDARD_PATHS.get(root)
        if paths is None:
            raise Tr069PathError(f"No standard paths for root '{root}'.")

        suffix_template = paths.get(canonical_name)
        if suffix_template is None:
            raise Tr069PathError(
                f"Unknown canonical parameter '{canonical_name}' "
                f"for data model '{root}'."
            )

        suffix = suffix_template.replace("{i}", str(instance_index))
        return f"{root}.{suffix}"

    def resolve_many(
        self,
        root: str,
        canonical_names: list[str],
        *,
        db: Session | None = None,
        vendor: str | None = None,
        model: str | None = None,
        instance_index: int = 1,
    ) -> dict[str, str]:
        """Resolve multiple canonical names. Returns {canonical_name: full_path}."""
        return {
            name: self.resolve(
                root,
                name,
                db=db,
                vendor=vendor,
                model=model,
                instance_index=instance_index,
            )
            for name in canonical_names
        }

    def build_params(
        self,
        root: str,
        param_values: dict[str, Any],
        *,
        db: Session | None = None,
        vendor: str | None = None,
        model: str | None = None,
        instance_index: int = 1,
    ) -> dict[str, Any]:
        """Resolve canonical names and pair with values.

        Args:
            param_values: ``{canonical_name: value}`` dict.

        Returns:
            ``{full_cwmp_path: value}`` dict ready for ``set_parameter_values()``.
        """
        result: dict[str, Any] = {}
        for canonical_name, value in param_values.items():
            path = self.resolve(
                root,
                canonical_name,
                db=db,
                vendor=vendor,
                model=model,
                instance_index=instance_index,
            )
            result[path] = value
        return result

    def resolve_object_base(self, root: str, object_type: str) -> str:
        """Return the base path prefix for enumerable objects.

        Args:
            root: "Device" or "InternetGatewayDevice".
            object_type: One of "ethernet_ports", "lan_hosts", "wifi_ssids".

        Returns:
            Base path (e.g. "Device.Ethernet.Interface.").
        """
        self._validate_root(root)
        bases = _OBJECT_BASES.get(root, {})
        suffix = bases.get(object_type)
        if suffix is None:
            raise Tr069PathError(
                f"Unknown object type '{object_type}' for root '{root}'."
            )
        return f"{root}.{suffix}"

    def has_canonical(self, root: str, canonical_name: str) -> bool:
        """Check if a canonical name is defined for the given root."""
        if root not in _VALID_ROOTS:
            return False
        paths = _STANDARD_PATHS.get(root, {})
        return canonical_name in paths

    def list_canonical_names(self, root: str | None = None) -> list[str]:
        """List all known canonical parameter names.

        If root is specified, returns names available for that root.
        If root is None, returns the union of both standards.
        """
        if root:
            return sorted(_STANDARD_PATHS.get(root, {}).keys())
        all_names: set[str] = set()
        for paths in _STANDARD_PATHS.values():
            all_names.update(paths.keys())
        return sorted(all_names)


# Singleton
tr069_path_resolver = Tr069PathResolver()
