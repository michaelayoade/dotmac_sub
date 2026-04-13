"""TR-069 parameter aggregation for ONT detail display.

Fetches and structures TR-069 parameters from GenieACS into sections
for display on the ONT detail page's TR-069 tab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.genieacs import GenieACSClient, GenieACSError
from app.services.network._common import normalize_mac_address
from app.services.network._resolve import resolve_genieacs

logger = logging.getLogger(__name__)

# TR-069 parameter path mappings.
# Both InternetGatewayDevice (TR-098) and Device (TR-181) roots are tried.
_IGD = "InternetGatewayDevice"
_DEV = "Device"

PARAM_GROUPS: dict[str, dict[str, list[str]]] = {
    "system": {
        "Manufacturer": [
            f"{_DEV}.DeviceInfo.Manufacturer",
            f"{_IGD}.DeviceInfo.Manufacturer",
        ],
        "Model": [f"{_DEV}.DeviceInfo.ModelName", f"{_IGD}.DeviceInfo.ModelName"],
        "Firmware": [
            f"{_DEV}.DeviceInfo.SoftwareVersion",
            f"{_IGD}.DeviceInfo.SoftwareVersion",
        ],
        "Hardware": [
            f"{_DEV}.DeviceInfo.HardwareVersion",
            f"{_IGD}.DeviceInfo.HardwareVersion",
        ],
        "Serial": [
            f"{_DEV}.DeviceInfo.SerialNumber",
            f"{_IGD}.DeviceInfo.SerialNumber",
        ],
        "Uptime": [f"{_DEV}.DeviceInfo.UpTime", f"{_IGD}.DeviceInfo.UpTime"],
        "CPU Usage": [f"{_DEV}.DeviceInfo.ProcessStatus.CPUUsage"],
        "Memory Total": [
            f"{_DEV}.DeviceInfo.MemoryStatus.Total",
            f"{_IGD}.DeviceInfo.MemoryStatus.Total",
        ],
        "Memory Free": [
            f"{_DEV}.DeviceInfo.MemoryStatus.Free",
            f"{_IGD}.DeviceInfo.MemoryStatus.Free",
        ],
        "MAC Address": [
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.MACAddress",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.MACAddress",
            f"{_DEV}.Ethernet.Interface.1.MACAddress",
            f"{_IGD}.LANDevice.1.LANEthernetInterfaceConfig.1.MACAddress",
        ],
    },
    "wan": {
        "Connection Type": [
            # Wildcard paths to match any WAN index
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ConnectionType",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ConnectionType",
            # Legacy fixed paths for backwards compatibility
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionType",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionType",
            f"{_DEV}.PPP.Interface.1.ConnectionStatus",
        ],
        "WAN IP": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ExternalIPAddress",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress",
            f"{_DEV}.IP.Interface.1.IPv4Address.1.IPAddress",
            f"{_DEV}.DHCPv4.Client.1.IPAddress",
        ],
        "Username": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
            f"{_DEV}.PPP.Interface.1.Username",
        ],
        "Status": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ConnectionStatus",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ConnectionStatus",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionStatus",
            f"{_DEV}.IP.Interface.1.Status",
        ],
        "Uptime": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Uptime",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Uptime",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.Uptime",
        ],
        "DNS Servers": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.DNSServers",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.DNSServers",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.DNSServers",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.DNSServers",
            f"{_DEV}.DNS.Client.Server.1.DNSServer",
        ],
        "Gateway": [
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.DefaultGateway",
            f"{_IGD}.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.DefaultGateway",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.DefaultGateway",
            f"{_IGD}.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.DefaultGateway",
            f"{_DEV}.Routing.Router.1.IPv4Forwarding.1.GatewayIPAddress",
        ],
    },
    "lan": {
        "LAN IP": [
            f"{_IGD}.LANDevice.*.LANHostConfigManagement.IPInterface.*.IPInterfaceIPAddress",
            f"{_IGD}.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
            f"{_DEV}.IP.Interface.2.IPv4Address.1.IPAddress",
        ],
        "Subnet Mask": [
            f"{_IGD}.LANDevice.*.LANHostConfigManagement.IPInterface.*.IPInterfaceSubnetMask",
            f"{_IGD}.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
            f"{_DEV}.IP.Interface.2.IPv4Address.1.SubnetMask",
        ],
        "DHCP Enabled": [
            f"{_IGD}.LANDevice.*.LANHostConfigManagement.DHCPServerEnable",
            f"{_IGD}.LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
            f"{_DEV}.DHCPv4.Server.Enable",
        ],
        "DHCP Start": [
            f"{_IGD}.LANDevice.*.LANHostConfigManagement.MinAddress",
            f"{_IGD}.LANDevice.1.LANHostConfigManagement.MinAddress",
            f"{_DEV}.DHCPv4.Server.Pool.1.MinAddress",
        ],
        "DHCP End": [
            f"{_IGD}.LANDevice.*.LANHostConfigManagement.MaxAddress",
            f"{_IGD}.LANDevice.1.LANHostConfigManagement.MaxAddress",
            f"{_DEV}.DHCPv4.Server.Pool.1.MaxAddress",
        ],
        "Connected Hosts": [
            f"{_IGD}.LANDevice.*.Hosts.HostNumberOfEntries",
            f"{_IGD}.LANDevice.1.Hosts.HostNumberOfEntries",
            f"{_DEV}.Hosts.HostNumberOfEntries",
        ],
    },
    "wireless": {
        "Enabled": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.Enable",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.Enable",
            f"{_DEV}.WiFi.SSID.1.Enable",
        ],
        "SSID": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.SSID",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.SSID",
            f"{_DEV}.WiFi.SSID.1.SSID",
        ],
        "Channel": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.Channel",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.Channel",
            f"{_DEV}.WiFi.Radio.1.Channel",
        ],
        "Standard": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.Standard",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.Standard",
            f"{_DEV}.WiFi.Radio.1.OperatingStandards",
        ],
        "Security Mode": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.BeaconType",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.BeaconType",
            f"{_DEV}.WiFi.AccessPoint.1.Security.ModeEnabled",
        ],
        "Connected Clients": [
            f"{_IGD}.LANDevice.*.WLANConfiguration.*.TotalAssociations",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.TotalAssociations",
            f"{_DEV}.WiFi.AccessPoint.1.AssociatedDeviceNumberOfEntries",
        ],
        "Password": [
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
            f"{_IGD}.LANDevice.1.WLANConfiguration.1.KeyPassphrase",
            f"{_DEV}.WiFi.AccessPoint.1.Security.KeyPassphrase",
        ],
    },
}

# Ethernet port object paths (we enumerate ports 1-4)
_ETH_PORT_PATHS_IGD = (
    "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.{i}."
)
_ETH_PORT_PATHS_DEV = "Device.Ethernet.Interface.{i}."
_ETH_FIELDS = ["Enable", "Status", "MaxBitRate", "DuplexMode", "MACAddress"]

# LAN host paths
_HOSTS_PATH_IGD = "InternetGatewayDevice.LANDevice.1.Hosts.Host."
_HOSTS_PATH_DEV = "Device.Hosts.Host."
_HOST_FIELDS = ["HostName", "IPAddress", "MACAddress", "InterfaceType", "Active"]


@dataclass
class TR069Summary:
    """Structured TR-069 data grouped by section."""

    ont_id: str | None = None
    system: dict[str, Any] = field(default_factory=dict)
    wan: dict[str, Any] = field(default_factory=dict)
    lan: dict[str, Any] = field(default_factory=dict)
    wireless: dict[str, Any] = field(default_factory=dict)
    ethernet_ports: list[dict[str, Any]] = field(default_factory=list)
    lan_hosts: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    source: str = "live"
    fetched_at: datetime | None = None
    raw_device: dict[str, Any] | None = None
    recent_informs: list[Any] = field(default_factory=list)
    cached_parameters: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    error: str | None = None


def _resolve_param_paths_from_capability(
    db: Session | None,
    vendor: str | None,
    model: str | None,
    canonical_name: str,
) -> list[str] | None:
    """Resolve parameter paths from VendorModelCapability registry.

    Returns device-specific TR-069 paths if a matching capability exists,
    or None to fall back to hardcoded paths.
    """
    if not db or not vendor or not model:
        return None
    try:
        from app.services.network.vendor_capabilities import (
            tr069_parameter_maps,
            vendor_capabilities,
        )

        capability = vendor_capabilities.resolve_capability(db, vendor, model)
        if not capability:
            return None
        path = tr069_parameter_maps.resolve_path(db, str(capability.id), canonical_name)
        if path:
            return [path]
    except Exception:
        logger.debug(
            "Failed to resolve vendor-specific TR-069 path for %s/%s",
            vendor,
            model,
            exc_info=True,
        )
    return None


def _extract_wildcard_value(
    device: dict[str, Any],
    path_pattern: str,
) -> Any:
    """Search for a parameter value at any index in the device document.

    Supports paths like 'WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Status'
    where '*' matches any numeric index (1, 2, 3, etc.).

    Returns the first value found at any matching path.
    """

    parts = path_pattern.split(".")

    def search(current: Any, remaining_parts: list[str]) -> Any:
        if not remaining_parts:
            # Reached end of path - extract value
            if isinstance(current, dict) and "_value" in current:
                return current["_value"]
            return None

        if not isinstance(current, dict):
            return None

        part = remaining_parts[0]
        rest = remaining_parts[1:]

        if part == "*":
            # Wildcard - try all numeric keys
            for key in sorted(current.keys()):
                if key.isdigit():
                    result = search(current[key], rest)
                    if result is not None:
                        return result
            return None
        else:
            # Exact match
            if part in current:
                return search(current[part], rest)
            return None

    return search(device, parts)


def _extract_first(
    client: GenieACSClient,
    device: dict[str, Any],
    param_paths: list[str],
) -> Any:
    """Try multiple parameter paths, return first non-None value.

    Supports wildcard paths with '*' for any numeric index.
    """
    for path in param_paths:
        if "*" in path:
            # Use wildcard search
            val = _extract_wildcard_value(device, path)
        else:
            val = client.extract_parameter_value(device, path)
        if val is not None:
            return val
    return None


def _extract_group(
    client: GenieACSClient,
    device: dict[str, Any],
    group_name: str,
    *,
    db: Session | None = None,
    vendor: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Extract all parameters in a named group.

    If db/vendor/model are provided, attempts to resolve paths from the
    VendorModelCapability registry first, falling back to hardcoded PARAM_GROUPS.
    """
    group = PARAM_GROUPS.get(group_name, {})
    result: dict[str, Any] = {}
    for label, paths in group.items():
        # Try vendor-specific paths from parameter map
        canonical = f"{group_name}.{label.lower().replace(' ', '_')}"
        custom_paths = _resolve_param_paths_from_capability(
            db, vendor, model, canonical
        )
        effective_paths = custom_paths if custom_paths else paths
        result[label] = _extract_first(client, device, effective_paths)
    return result


def _extract_object_instances(
    device: dict[str, Any],
    base_path: str,
    fields: list[str],
    max_instances: int = 8,
) -> list[dict[str, Any]]:
    """Extract numbered object instances (e.g., Ethernet ports, LAN hosts).

    Navigates the GenieACS device document structure to find numbered
    sub-objects like Host.1., Host.2., etc.
    """
    results: list[dict[str, Any]] = []
    # Navigate to base object in device document
    parts = base_path.rstrip(".").split(".")
    current: Any = device
    for part in parts:
        if not isinstance(current, dict):
            return results
        current = current.get(part)
        if current is None:
            return results

    if not isinstance(current, dict):
        return results

    # Try numbered instances
    for i in range(1, max_instances + 1):
        instance = current.get(str(i))
        if not isinstance(instance, dict):
            continue
        row: dict[str, Any] = {"index": i}
        for f in fields:
            node = instance.get(f)
            if isinstance(node, dict) and "_value" in node:
                row[f] = node["_value"]
            else:
                row[f] = node
        results.append(row)
    return results


def _value_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "up"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "down"}:
        return False
    return None


def _normalize_ethernet_ports(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add stable display keys while preserving raw TR-069 fields."""
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        item = dict(row or {})
        enable = item.get("Enable")
        status = item.get("Status")
        speed = item.get("MaxBitRate")
        try:
            speed_mbps = int(str(speed).strip()) if speed not in (None, "") else None
        except (TypeError, ValueError):
            speed_mbps = None
        item.setdefault("port", item.get("index"))
        item.setdefault("admin_enabled", _value_to_bool(enable))
        item.setdefault("link_status", str(status or "unknown").strip() or "unknown")
        item.setdefault("speed_mbps", speed_mbps)
        item.setdefault("duplex", item.get("DuplexMode"))
        item.setdefault("mac_address", normalize_mac_address(item.get("MACAddress")))
        normalized.append(item)
    return normalized


def _normalize_lan_hosts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add stable display keys while preserving raw TR-069 fields."""
    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        item = dict(row or {})
        item.setdefault("host_name", item.get("HostName") or "")
        item.setdefault("ip_address", item.get("IPAddress") or "")
        item.setdefault("mac_address", normalize_mac_address(item.get("MACAddress")))
        item.setdefault("interface_type", item.get("InterfaceType") or "")
        item.setdefault("active", _value_to_bool(item.get("Active")))
        normalized.append(item)
    return normalized


class OntTR069:
    """Fetch and structure TR-069 parameters for ONT display."""

    @staticmethod
    def get_device_summary(
        db: Session,
        ont_id: str,
        *,
        persist_observed_runtime: bool = False,
    ) -> TR069Summary:
        """Return structured TR-069 data grouped by section.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            TR069Summary with grouped parameter data.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return TR069Summary(error="ONT not found.")

        resolved = resolve_genieacs(db, ont)
        if not resolved:
            cached_summary = OntTR069._summary_from_snapshot(ont)
            if cached_summary:
                cached_summary.error = (
                    "Showing cached TR-069 snapshot. "
                    "No live ACS device or server could be resolved."
                )
                OntTR069._attach_recent_informs(db, ont, cached_summary)
                OntTR069._attach_cached_parameters(db, ont, cached_summary)
                return cached_summary
            stored_summary = OntTR069._summary_from_stored_records(db, ont)
            if stored_summary:
                return stored_summary
            return TR069Summary(
                error="This device is not managed via TR-069. "
                "No matching CPE device or ACS server was found."
            )

        client, device_id = resolved
        try:
            device = client.get_device(device_id)
        except GenieACSError as e:
            logger.error("TR-069 fetch failed for ONT %s: %s", ont.serial_number, e)
            cached_summary = OntTR069._summary_from_snapshot(ont)
            if cached_summary:
                cached_summary.error = (
                    f"Showing cached TR-069 snapshot. Live fetch failed: {e}"
                )
                OntTR069._attach_recent_informs(db, ont, cached_summary)
                OntTR069._attach_cached_parameters(db, ont, cached_summary)
                return cached_summary
            stored_summary = OntTR069._summary_from_stored_records(db, ont)
            if stored_summary:
                stored_summary.error = (
                    f"Showing cached TR-069 records. Live fetch failed: {e}"
                )
                return stored_summary
            return TR069Summary(error=f"Failed to fetch TR-069 data: {e}")

        ont_vendor = getattr(ont, "vendor", None)
        ont_model = getattr(ont, "model", None)

        summary = TR069Summary(
            available=True,
            ont_id=str(ont.id),
            source="live",
            fetched_at=datetime.now(UTC),
            raw_device=device,
        )
        summary.system = _extract_group(
            client, device, "system", db=db, vendor=ont_vendor, model=ont_model
        )
        summary.wan = _extract_group(
            client, device, "wan", db=db, vendor=ont_vendor, model=ont_model
        )
        summary.lan = _extract_group(
            client, device, "lan", db=db, vendor=ont_vendor, model=ont_model
        )
        summary.wireless = _extract_group(
            client, device, "wireless", db=db, vendor=ont_vendor, model=ont_model
        )

        # Ethernet ports
        for base_path in [_ETH_PORT_PATHS_IGD, _ETH_PORT_PATHS_DEV]:
            # The path template has {i} — but we want the base object
            ports_base = base_path.split(".{i}")[0] + "."
            ports = _extract_object_instances(device, ports_base, _ETH_FIELDS)
            if ports:
                summary.ethernet_ports = _normalize_ethernet_ports(ports)
                break

        # LAN hosts
        for hosts_path in [_HOSTS_PATH_IGD, _HOSTS_PATH_DEV]:
            hosts = _extract_object_instances(device, hosts_path, _HOST_FIELDS)
            if hosts:
                summary.lan_hosts = _normalize_lan_hosts(hosts)
                break

        # Format uptime if present
        uptime_val = summary.system.get("Uptime")
        if uptime_val is not None:
            try:
                secs = int(uptime_val)
                days, remainder = divmod(secs, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes = remainder // 60
                summary.system["Uptime"] = f"{days}d {hours}h {minutes}m"
            except (ValueError, TypeError):
                pass

        # Format memory as percentage if both total and free are available
        mem_total = summary.system.get("Memory Total")
        mem_free = summary.system.get("Memory Free")
        if mem_total and mem_free:
            try:
                total = int(mem_total)
                free = int(mem_free)
                if total > 0:
                    used_pct = ((total - free) / total) * 100
                    summary.system["Memory Usage"] = (
                        f"{used_pct:.1f}% ({free:,} / {total:,} KB)"
                    )
            except (ValueError, TypeError):
                pass

        if persist_observed_runtime:
            OntTR069._persist_observed_runtime(db, ont, summary)

        OntTR069._attach_recent_informs(db, ont, summary)
        OntTR069._attach_cached_parameters(db, ont, summary)
        return summary

    @staticmethod
    def _summary_from_stored_records(
        db: Session,
        ont: OntUnit,
    ) -> TR069Summary | None:
        summary = TR069Summary(
            available=True,
            ont_id=str(ont.id),
            source="cache",
            fetched_at=getattr(ont, "tr069_last_snapshot_at", None)
            or getattr(ont, "observed_runtime_updated_at", None),
            error=(
                "Showing cached TR-069 records. "
                "No live ACS device or snapshot could be resolved."
            ),
        )
        OntTR069._attach_recent_informs(db, ont, summary)
        OntTR069._attach_cached_parameters(db, ont, summary)
        if summary.recent_informs or summary.cached_parameters:
            return summary
        return None

    @staticmethod
    def _attach_recent_informs(
        db: Session,
        ont: OntUnit,
        summary: TR069Summary,
        *,
        limit: int = 10,
    ) -> None:
        from app.models.tr069 import Tr069CpeDevice, Tr069Session

        sessions = (
            db.query(Tr069Session)
            .join(Tr069CpeDevice, Tr069Session.device_id == Tr069CpeDevice.id)
            .filter(Tr069CpeDevice.ont_unit_id == ont.id)
            .filter(Tr069CpeDevice.is_active.is_(True))
            .order_by(Tr069Session.started_at.desc(), Tr069Session.created_at.desc())
            .limit(limit)
            .all()
        )
        summary.recent_informs = sessions

    @staticmethod
    def _parameter_group(name: str) -> str:
        lowered = name.lower()
        if ".wan" in lowered or ".ppp" in lowered or ".managementserver." in lowered:
            return "WAN / ACS"
        if ".wifi" in lowered or ".wlan" in lowered:
            return "Wireless"
        if ".lan" in lowered or ".dhcp" in lowered or ".hosts." in lowered:
            return "LAN"
        if ".ethernet." in lowered:
            return "Ethernet"
        if ".deviceinfo." in lowered or ".devicemanagement." in lowered:
            return "System"
        return "Other"

    @staticmethod
    def _attach_cached_parameters(
        db: Session,
        ont: OntUnit,
        summary: TR069Summary,
        *,
        limit: int = 200,
    ) -> None:
        from app.models.tr069 import Tr069CpeDevice, Tr069Parameter

        params = (
            db.query(Tr069Parameter)
            .join(Tr069CpeDevice, Tr069Parameter.device_id == Tr069CpeDevice.id)
            .filter(Tr069CpeDevice.ont_unit_id == ont.id)
            .filter(Tr069CpeDevice.is_active.is_(True))
            .order_by(Tr069Parameter.updated_at.desc(), Tr069Parameter.name.asc())
            .limit(limit)
            .all()
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for param in params:
            group = OntTR069._parameter_group(param.name)
            grouped.setdefault(group, []).append(
                {
                    "name": param.name,
                    "value": param.value,
                    "updated_at": param.updated_at,
                }
            )
        summary.cached_parameters = grouped
        OntTR069._populate_summary_from_cached_parameters(summary)

    @staticmethod
    def _set_missing(target: dict[str, Any], key: str, value: Any) -> None:
        if value in (None, ""):
            return
        if target.get(key) in (None, ""):
            target[key] = value

    @staticmethod
    def _populate_summary_from_cached_parameters(summary: TR069Summary) -> None:
        """Backfill friendly summary fields from cached inform parameters."""
        for parameters in summary.cached_parameters.values():
            for param in parameters:
                name = str(param.get("name") or "")
                lowered = name.lower()
                value = param.get("value")
                if lowered.endswith(".ssid") and (
                    ".wlanconfiguration." in lowered or ".wifi.ssid." in lowered
                ):
                    OntTR069._set_missing(summary.wireless, "SSID", value)
                elif lowered.endswith(".totalassociations") or lowered.endswith(
                    ".associateddevicenumberofentries"
                ):
                    OntTR069._set_missing(
                        summary.wireless,
                        "Connected Clients",
                        value,
                    )
                elif lowered.endswith(".hostnumberofentries"):
                    OntTR069._set_missing(summary.lan, "Connected Hosts", value)

    @staticmethod
    def _snapshot_payload(summary: TR069Summary) -> dict[str, Any]:
        fetched_at = summary.fetched_at or datetime.now(UTC)
        return {
            "ont_id": summary.ont_id,
            "system": summary.system,
            "wan": summary.wan,
            "lan": summary.lan,
            "wireless": summary.wireless,
            "ethernet_ports": summary.ethernet_ports,
            "lan_hosts": summary.lan_hosts,
            "fetched_at": fetched_at.isoformat(),
            "raw_device": summary.raw_device,
        }

    @staticmethod
    def _summary_from_snapshot(ont: OntUnit) -> TR069Summary | None:
        snapshot = getattr(ont, "tr069_last_snapshot", None)
        if not isinstance(snapshot, dict) or not snapshot:
            return None

        fetched_at = None
        fetched_at_raw = snapshot.get("fetched_at")
        if isinstance(fetched_at_raw, str) and fetched_at_raw:
            try:
                fetched_at = datetime.fromisoformat(fetched_at_raw)
            except ValueError:
                fetched_at = None

        return TR069Summary(
            ont_id=str(ont.id),
            system=dict(snapshot.get("system") or {}),
            wan=dict(snapshot.get("wan") or {}),
            lan=dict(snapshot.get("lan") or {}),
            wireless=dict(snapshot.get("wireless") or {}),
            ethernet_ports=_normalize_ethernet_ports(
                list(snapshot.get("ethernet_ports") or [])
            ),
            lan_hosts=_normalize_lan_hosts(list(snapshot.get("lan_hosts") or [])),
            available=True,
            source="cache",
            fetched_at=fetched_at
            or getattr(ont, "tr069_last_snapshot_at", None)
            or getattr(ont, "observed_runtime_updated_at", None),
            raw_device=snapshot.get("raw_device")
            if isinstance(snapshot.get("raw_device"), dict)
            else None,
        )

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_bool(value: Any) -> bool | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        return None

    @staticmethod
    def _choose_mac_address(summary: TR069Summary) -> str | None:
        candidates: list[str] = []
        if summary.system:
            system_mac = normalize_mac_address(summary.system.get("MAC Address"))
            if system_mac:
                candidates.append(system_mac)
        for port in summary.ethernet_ports or []:
            port_mac = normalize_mac_address((port or {}).get("MACAddress"))
            if port_mac:
                candidates.append(port_mac)
        if not candidates:
            return None
        return sorted(set(candidates))[-1]

    @staticmethod
    def _persist_observed_runtime(
        db: Session,
        ont: OntUnit,
        summary: TR069Summary,
        *,
        commit: bool = True,
    ) -> None:
        """Persist useful runtime/observed fields back onto OntUnit."""
        if not summary.available:
            return

        tr069_serial = (
            str(summary.system.get("Serial") or "").strip() if summary.system else ""
        )
        if tr069_serial:
            current_serial = str(getattr(ont, "serial_number", "") or "")
            is_synthetic = (
                not current_serial
                or current_serial.startswith("HW-")
                or current_serial.startswith("ZT-")
                or current_serial.startswith("NK-")
                or current_serial.startswith("OLT-")
            )
            if is_synthetic:
                # Replace synthetic SNMP serial with real TR-069 serial
                logger.info(
                    "ONT %s: replacing synthetic serial '%s' with TR-069 serial '%s'",
                    ont.id,
                    current_serial,
                    tr069_serial[:120],
                )
                ont.serial_number = tr069_serial[:120]
            elif current_serial != tr069_serial[:120]:
                # Hardware replacement detected - real serial changed
                logger.warning(
                    "ONT %s HARDWARE CHANGE DETECTED: serial changed from '%s' to '%s' "
                    "(position: %s, external_id: %s)",
                    ont.id,
                    current_serial,
                    tr069_serial[:120],
                    getattr(ont, "board", "?") or "?",
                    getattr(ont, "external_id", "?") or "?",
                )
                ont.serial_number = tr069_serial[:120]

        mac_address = OntTR069._choose_mac_address(summary)

        wan_ip = str(summary.wan.get("WAN IP") or "").strip() if summary.wan else ""
        pppoe_status = (
            str(summary.wan.get("Status") or "").strip() if summary.wan else ""
        )

        wifi_clients = OntTR069._to_int(
            summary.wireless.get("Connected Clients") if summary.wireless else None
        )
        lan_hosts_count = (
            len(summary.lan_hosts)
            if summary.lan_hosts
            else OntTR069._to_int(
                summary.lan.get("Connected Hosts") if summary.lan else None
            )
        )

        dhcp_enabled = OntTR069._to_bool(
            summary.lan.get("DHCP Enabled") if summary.lan else None
        )
        lan_mode = (
            "router"
            if dhcp_enabled is True
            else "bridge"
            if dhcp_enabled is False
            else None
        )

        if mac_address:
            ont.mac_address = mac_address
        if wan_ip:
            ont.observed_wan_ip = wan_ip
        if pppoe_status:
            ont.observed_pppoe_status = pppoe_status
        if lan_mode:
            ont.observed_lan_mode = lan_mode
        if wifi_clients is not None:
            ont.observed_wifi_clients = wifi_clients
        if lan_hosts_count is not None:
            ont.observed_lan_hosts = lan_hosts_count
        observed_at = summary.fetched_at or datetime.now(UTC)
        ont.observed_runtime_updated_at = observed_at
        ont.tr069_last_snapshot = OntTR069._snapshot_payload(summary)
        ont.tr069_last_snapshot_at = observed_at

        db.add(ont)
        if commit:
            db.commit()
            db.refresh(ont)
        else:
            db.flush()
        if getattr(ont, "is_active", False):
            from app.services.network.cpe import ensure_cpe_for_ont

            ensure_cpe_for_ont(db, ont, commit=commit)

    @staticmethod
    def get_lan_hosts(db: Session, ont_id: str) -> list[dict[str, Any]]:
        """Return connected LAN hosts for an ONT.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            List of host dicts with hostname, ip, mac, interface, active.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return []

        resolved = resolve_genieacs(db, ont)
        if not resolved:
            return []

        client, device_id = resolved
        try:
            device = client.get_device(device_id)
        except GenieACSError:
            return []

        for hosts_path in [_HOSTS_PATH_IGD, _HOSTS_PATH_DEV]:
            hosts = _extract_object_instances(device, hosts_path, _HOST_FIELDS)
            if hosts:
                return _normalize_lan_hosts(hosts)
        return []

    @staticmethod
    def get_ethernet_ports(db: Session, ont_id: str) -> list[dict[str, Any]]:
        """Return Ethernet port status for an ONT.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            List of port dicts with port, admin_enabled, status, speed, duplex.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return []

        resolved = resolve_genieacs(db, ont)
        if not resolved:
            return []

        client, device_id = resolved
        try:
            device = client.get_device(device_id)
        except GenieACSError:
            return []

        for base_path in [_ETH_PORT_PATHS_IGD, _ETH_PORT_PATHS_DEV]:
            ports_base = base_path.split(".{i}")[0] + "."
            ports = _extract_object_instances(device, ports_base, _ETH_FIELDS)
            if ports:
                return _normalize_ethernet_ports(ports)
        return []


ont_tr069 = OntTR069()
