"""Live per-interface bandwidth for monitored core-router interfaces.

Reads VictoriaMetrics: the native infrastructure poller pushes raw
IF-MIB octet counters for every admin-monitored interface
(``infrastructure_polling.push_interface_counters``), and this service derives
bps with ``rate()`` at query time. Read-only and on-demand — no DB writes, no
scheduled job — with an 8-second in-memory cache so a 10-second admin-page
poll doesn't hammer the TSDB.

Interfaces are matched by the ``interface_id`` label the poller stamps on
every sample, so there is no host mapping to resolve.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceInterface, NetworkDevice

logger = logging.getLogger(__name__)

VICTORIAMETRICS_URL = os.getenv("VICTORIAMETRICS_URL", "http://victoriametrics:8428")

_CACHE_TTL_SECONDS = 8.0
_QUERY_TIMEOUT_SECONDS = 3.0
# rate() window: the poller pushes counters every beat run (default 60s), so
# 5 minutes always spans several samples without smearing short bursts too far.
_RATE_WINDOW = "5m"

_IN_METRIC = "core_interface_in_octets_total"
_OUT_METRIC = "core_interface_out_octets_total"


class MetricsQueryError(RuntimeError):
    """A VictoriaMetrics response that isn't a usable query result (non-success
    status or malformed JSON) — distinct from transport failures (httpx)."""


@dataclass(frozen=True)
class InterfaceBandwidth:
    """Latest snapshot for a single monitored interface."""

    rx_bps: float | None
    tx_bps: float | None
    last_clock: int | None  # unix seconds; max(rx_clock, tx_clock)


@dataclass(frozen=True)
class CoreRouterBandwidth:
    """Per-device result envelope.

    `error` is set when the metrics query failed (config missing, network);
    `by_interface_id` is keyed by DeviceInterface.id and contains entries only
    for the interfaces we successfully fetched data for. Callers should render
    "--" for any monitored interface not present in the map.
    """

    by_interface_id: dict[str, InterfaceBandwidth]
    fetched_at: float
    error: str | None = None


_bandwidth_cache: dict[
    str, tuple[CoreRouterBandwidth, float]
] = {}  # device.id → (result, ts)
_lock = threading.Lock()

_client: httpx.Client | None = None


def _now() -> float:
    return time.monotonic()


def _get_client() -> httpx.Client:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(timeout=_QUERY_TIMEOUT_SECONDS)
    return _client


def _query_bps(device_id: str, metric: str) -> dict[str, tuple[float, int]]:
    """Instant-query bps per interface_id for one counter metric.

    Returns ``{interface_id: (bps, sample_unix_seconds)}``. Raises
    ``httpx.HTTPError`` on transport/HTTP failures.
    """
    query = f'rate({metric}{{device_id="{device_id}"}}[{_RATE_WINDOW}]) * 8'
    response = _get_client().get(
        f"{VICTORIAMETRICS_URL}/api/v1/query", params={"query": query}
    )
    response.raise_for_status()
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise MetricsQueryError(f"malformed VictoriaMetrics response: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        status = payload.get("status") if isinstance(payload, dict) else type(payload)
        raise MetricsQueryError(f"VictoriaMetrics query status: {status}")
    results: dict[str, tuple[float, int]] = {}
    for series in (payload.get("data") or {}).get("result") or []:
        interface_id = (series.get("metric") or {}).get("interface_id")
        value = series.get("value") or []
        if not interface_id or len(value) != 2:
            continue
        try:
            results[str(interface_id)] = (float(value[1]), int(float(value[0])))
        except (TypeError, ValueError):
            continue
    return results


def get_interface_bandwidth(
    db: Session,
    device: NetworkDevice,
    interfaces: list[DeviceInterface],
) -> CoreRouterBandwidth:
    """Fetch latest in/out bps from VictoriaMetrics for monitored interfaces.

    Caches per-device for ~8 seconds. Returns a result with `error` populated
    rather than raising, so callers can render "--" without aborting the page.
    """
    _ = db  # kept for signature stability; no lookup needed with direct labels
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

    if not VICTORIAMETRICS_URL:
        return CoreRouterBandwidth(
            by_interface_id={},
            fetched_at=time.time(),
            error="Live monitoring not configured",
        )

    try:
        rx_by_iface = _query_bps(str(device.id), _IN_METRIC)
        tx_by_iface = _query_bps(str(device.id), _OUT_METRIC)
    except (httpx.HTTPError, MetricsQueryError) as exc:
        logger.info("Live bandwidth fetch failed for %s: %s", device.name, exc)
        return CoreRouterBandwidth(
            by_interface_id={},
            fetched_at=time.time(),
            error="Live monitoring unavailable",
        )

    by_iface_id: dict[str, InterfaceBandwidth] = {}
    for iface in monitored:
        iface_id = str(iface.id)
        rx_pair = rx_by_iface.get(iface_id)
        tx_pair = tx_by_iface.get(iface_id)
        if rx_pair is None and tx_pair is None:
            continue
        rx_clock = rx_pair[1] if rx_pair else 0
        tx_clock = tx_pair[1] if tx_pair else 0
        by_iface_id[iface_id] = InterfaceBandwidth(
            rx_bps=rx_pair[0] if rx_pair else None,
            tx_bps=tx_pair[0] if tx_pair else None,
            last_clock=max(rx_clock, tx_clock) or None,
        )

    # Distinguish "the poller has pushed nothing for this device" from a
    # transient empty response — admins need to know when SNMP polling isn't
    # producing counters (device snmp_enabled off, agent not answering, or the
    # rate() window hasn't seen two samples yet).
    error: str | None = None
    if not rx_by_iface and not tx_by_iface:
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
