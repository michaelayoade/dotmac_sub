"""TR-069 parameter aggregation for CPE device detail display.

Fetches and structures TR-069 parameters from GenieACS into sections
for display on the CPE detail page's TR-069 tab.  Reuses PARAM_GROUPS
and helpers from ont_tr069 but omits optical and PPPoE-specific sections.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import CPEDevice
from app.services.genieacs import GenieACSError
from app.services.network._resolve import resolve_genieacs_for_cpe
from app.services.network.ont_tr069 import (
    _ETH_FIELDS,
    _ETH_PORT_PATHS_DEV,
    _ETH_PORT_PATHS_IGD,
    _HOST_FIELDS,
    _HOSTS_PATH_DEV,
    _HOSTS_PATH_IGD,
    _extract_group,
    _extract_object_instances,
)

logger = logging.getLogger(__name__)


@dataclass
class CpeTR069Summary:
    """Structured TR-069 data grouped by section for a CPE device."""

    cpe_id: str | None = None
    system: dict[str, Any] = field(default_factory=dict)
    wan: dict[str, Any] = field(default_factory=dict)
    lan: dict[str, Any] = field(default_factory=dict)
    wireless: dict[str, Any] = field(default_factory=dict)
    ethernet_ports: list[dict[str, Any]] = field(default_factory=list)
    lan_hosts: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    error: str | None = None


class CpeTR069:
    """Fetch and structure TR-069 parameters for CPE device display."""

    @staticmethod
    def get_device_summary(db: Session, cpe_id: str) -> CpeTR069Summary:
        """Return structured TR-069 data grouped by section.

        Args:
            db: Database session.
            cpe_id: CPEDevice ID.

        Returns:
            CpeTR069Summary with grouped parameter data.
        """
        cpe = db.get(CPEDevice, cpe_id)
        if not cpe:
            return CpeTR069Summary(error="CPE device not found.")

        resolved = resolve_genieacs_for_cpe(db, cpe)
        if not resolved:
            return CpeTR069Summary(
                error="This device is not managed via TR-069. "
                "No matching CPE device or ACS server was found."
            )

        client, device_id = resolved
        try:
            device = client.get_device(device_id)
        except GenieACSError as e:
            logger.error("TR-069 fetch failed for CPE %s: %s", cpe.serial_number, e)
            return CpeTR069Summary(error=f"Failed to fetch TR-069 data: {e}")

        cpe_vendor = getattr(cpe, "vendor", None)
        cpe_model = getattr(cpe, "model", None)

        summary = CpeTR069Summary(available=True, cpe_id=str(cpe.id))
        summary.system = _extract_group(
            client, device, "system", db=db, vendor=cpe_vendor, model=cpe_model
        )
        summary.wan = _extract_group(
            client, device, "wan", db=db, vendor=cpe_vendor, model=cpe_model
        )
        summary.lan = _extract_group(
            client, device, "lan", db=db, vendor=cpe_vendor, model=cpe_model
        )
        summary.wireless = _extract_group(
            client, device, "wireless", db=db, vendor=cpe_vendor, model=cpe_model
        )

        # Ethernet ports
        for base_path in [_ETH_PORT_PATHS_IGD, _ETH_PORT_PATHS_DEV]:
            ports_base = base_path.split(".{i}")[0] + "."
            ports = _extract_object_instances(device, ports_base, _ETH_FIELDS)
            if ports:
                summary.ethernet_ports = ports
                break

        # LAN hosts
        for hosts_path in [_HOSTS_PATH_IGD, _HOSTS_PATH_DEV]:
            hosts = _extract_object_instances(device, hosts_path, _HOST_FIELDS)
            if hosts:
                summary.lan_hosts = hosts
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
                    summary.system["Memory Usage"] = f"{used_pct:.1f}% ({free:,} / {total:,} KB)"
            except (ValueError, TypeError):
                pass

        return summary


cpe_tr069 = CpeTR069()
