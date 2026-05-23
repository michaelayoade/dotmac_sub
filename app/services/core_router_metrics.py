"""Live per-interface bandwidth lookup from Zabbix for monitored core-router interfaces.

This is a read-only on-demand probe — no DB writes, no scheduled job. It returns
the latest in/out bps values Zabbix has on file for interfaces that admins have
explicitly enabled monitoring on (DeviceInterface.monitored=True).

Stays small on purpose: per-device call is one item.get against Zabbix, plus an
8-second in-memory cache so a 10-second admin-page poll doesn't hammer the API.

NetworkDevice ↔ Zabbix host mapping uses the existing NasDevice.zabbix_host_id
foreign key maintained by zabbix_host_sync.py — no string matching, no IP guess.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice
from app.models.network_monitoring import DeviceInterface, NetworkDevice
from app.services.zabbix import (
    ZabbixClient,
    ZabbixClientError,
    ZabbixConfigurationError,
)

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 8.0


@dataclass(frozen=True)
class InterfaceBandwidth:
    """Latest snapshot for a single monitored interface."""

    rx_bps: float | None
    tx_bps: float | None
    last_clock: int | None  # unix seconds; max(rx_clock, tx_clock)


@dataclass(frozen=True)
class CoreRouterBandwidth:
    """Per-device result envelope.

    `error` is set when the Zabbix call failed (config missing, network, auth);
    `by_interface_id` is keyed by DeviceInterface.id and contains entries only
    for the interfaces we successfully fetched data for. Callers should render
    "--" for any monitored interface not present in the map.
    """

    by_interface_id: dict[str, InterfaceBandwidth]
    fetched_at: float
    error: str | None = None


# Mikrotik / SNMP interface index ↔ Zabbix item key parser.
# Matches both `net.if.in[ifHCInOctets.41]` and `net.if.out[ifHCOutOctets.41]`.
_IF_KEY_RE = re.compile(r"net\.if\.(in|out)\[[^.]+\.(\d+)\]")

_bandwidth_cache: dict[str, tuple[CoreRouterBandwidth, float]] = {}  # device.id → (result, ts)
_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _resolve_hostid(db: Session, device: NetworkDevice) -> str | None:
    """Resolve a NetworkDevice to its Zabbix host id via the NasDevice FK.

    Uses NasDevice.zabbix_host_id, which is the explicit mapping maintained by
    zabbix_host_sync. Returns None if no NasDevice matches the mgmt_ip or the
    NasDevice exists but hasn't been pushed to Zabbix yet.
    """
    if not device.mgmt_ip:
        return None
    nas = db.scalars(
        select(NasDevice)
        .where(NasDevice.is_active.is_(True))
        .where(
            or_(
                NasDevice.management_ip == device.mgmt_ip,
                NasDevice.ip_address == device.mgmt_ip,
            )
        )
    ).first()
    if nas is None or not nas.zabbix_host_id:
        return None
    return str(nas.zabbix_host_id)


def _parse_items_by_snmp_index(
    items: list[dict[str, Any]],
) -> dict[int, dict[str, tuple[float, int]]]:
    """Group Zabbix items by snmp_index, splitting in vs out.

    Returns {snmp_index: {"in": (value, clock), "out": (value, clock)}}.
    """
    grouped: dict[int, dict[str, tuple[float, int]]] = {}
    for item in items:
        key = item.get("key_") or ""
        match = _IF_KEY_RE.search(key)
        if not match:
            continue
        direction, idx_str = match.group(1), match.group(2)
        try:
            idx = int(idx_str)
            value = float(item.get("lastvalue") or 0)
            clock = int(item.get("lastclock") or 0)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(idx, {})[direction] = (value, clock)
    return grouped


def get_interface_bandwidth(
    db: Session,
    device: NetworkDevice,
    interfaces: list[DeviceInterface],
) -> CoreRouterBandwidth:
    """Fetch latest in/out bps from Zabbix for the device's monitored interfaces.

    Caches per-device for ~8 seconds. Returns a result with `error` populated
    rather than raising, so callers can render "--" without aborting the page.
    """
    cache_key = str(device.id)
    cached = _bandwidth_cache.get(cache_key)
    if cached and (_now() - cached[1] < _CACHE_TTL_SECONDS):
        return cached[0]

    monitored = [i for i in interfaces if i.monitored and i.snmp_index is not None]
    if not monitored:
        result = CoreRouterBandwidth(by_interface_id={}, fetched_at=time.time())
        with _lock:
            _bandwidth_cache[cache_key] = (result, _now())
        return result

    hostid = _resolve_hostid(db, device)
    if not hostid:
        return CoreRouterBandwidth(
            by_interface_id={},
            fetched_at=time.time(),
            error="Device not linked to live monitoring",
        )

    try:
        client = ZabbixClient.from_env()
    except ZabbixConfigurationError as exc:
        logger.warning("Live monitoring not configured: %s", exc)
        return CoreRouterBandwidth(
            by_interface_id={}, fetched_at=time.time(), error="Live monitoring not configured"
        )

    try:
        items = client.get_items(host_ids=[hostid], metric="net.if")
    except ZabbixClientError as exc:
        logger.info("Live bandwidth fetch failed for %s: %s", device.name, exc)
        return CoreRouterBandwidth(
            by_interface_id={}, fetched_at=time.time(), error="Live monitoring unavailable"
        )

    by_idx = _parse_items_by_snmp_index(items)
    by_iface_id: dict[str, InterfaceBandwidth] = {}
    for iface in monitored:
        entry = by_idx.get(int(iface.snmp_index)) if iface.snmp_index is not None else None
        if not entry:
            continue
        rx_pair = entry.get("in")
        tx_pair = entry.get("out")
        rx_clock = rx_pair[1] if rx_pair else 0
        tx_clock = tx_pair[1] if tx_pair else 0
        by_iface_id[str(iface.id)] = InterfaceBandwidth(
            rx_bps=rx_pair[0] if rx_pair else None,
            tx_bps=tx_pair[0] if tx_pair else None,
            last_clock=max(rx_clock, tx_clock) or None,
        )

    # Distinguish "Zabbix has nothing for this host" from a transient empty
    # response — admins need to know if their template lacks interface items.
    error: str | None = None
    if not by_idx:
        error = "Interface counters not enabled in monitoring"

    result = CoreRouterBandwidth(
        by_interface_id=by_iface_id, fetched_at=time.time(), error=error
    )
    with _lock:
        _bandwidth_cache[cache_key] = (result, _now())
    return result


def invalidate_cache(device_id: str | None = None) -> None:
    """Drop the per-device cache. Use after a monitoring toggle so the next page
    load reflects the new set immediately rather than waiting up to 8 s."""
    with _lock:
        if device_id is None:
            _bandwidth_cache.clear()
        else:
            _bandwidth_cache.pop(str(device_id), None)
