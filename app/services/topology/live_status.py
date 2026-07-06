"""Topology live-status warmer (Phase 3).

Batch-fetches Zabbix host availability for reconciled nodes and writes a coarse
``live_status`` (up/down/unknown) into the network_devices cache. A failed
Zabbix ICMP ping item is always the primary outage signal; for enabled hosts,
ICMP success is also the primary healthy signal. SNMP/host availability is used
only when there is no authoritative ICMP result. The Network Path panel reads
that cache — Zabbix is NEVER called on the request path (same warm-and-store
pattern as
``monitoring_warm``).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import NetworkDevice
from app.services.topology.zabbix_reconcile import SOURCE

UP = "up"
DOWN = "down"
PROBLEM = "problem"
UNKNOWN = "unknown"

_CHUNK = 200

# Heartbeat written on every warm run so the customer-facing connection-status
# reader can tell whether live_status is being refreshed. If the warmer dies,
# this key ages out and good states stop being trusted (see topology.selfcare).
# TTL is far longer than the staleness window so the timestamp survives to be
# age-compared (a TTL-expired key reads as "missing", which we treat as
# unknown-freshness, not stale — see selfcare._warm_is_stale).
WARM_HEARTBEAT_KEY = "topology:live_status:warmed_at"
_WARM_HEARTBEAT_TTL_SECONDS = 86_400


def touch_warm_heartbeat(now: datetime | None = None) -> None:
    """Record that the live_status warmer just ran (advisory, cache-only).

    Called from the warm task after a successful refresh — kept out of the pure
    ``warm_topology_status`` service function so that has no cache side effects.
    """
    try:
        from app.services.app_cache import set_json

        stamp = (now or _now()).isoformat()
        set_json(WARM_HEARTBEAT_KEY, stamp, _WARM_HEARTBEAT_TTL_SECONDS)
    except Exception:  # cache is advisory; never fail the warm over it
        pass


def _now() -> datetime:
    return datetime.now(UTC)


def _sla_log_enabled() -> bool:
    try:
        from app.config import settings

        return bool(settings.sla_availability_log_enabled)
    except Exception:  # config is advisory here; never fail the warm over it
        return False


def _coverage():
    """Monitoring-path coverage for SLA-bridge gating; None on any failure
    (then the bridge logs everything, i.e. pre-Phase-3 behaviour)."""
    try:
        from app.services.monitoring_coverage import get_coverage

        return get_coverage()
    except Exception:
        return None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _availability(zhost: dict) -> str:
    """Map Zabbix availability to up/down/unknown.

    Prefers an explicit host-level ``available`` (1 up, 2 down); falls back to
    interface availability (main interface first) for Zabbix 6+ where host-level
    availability was removed.

    A host Zabbix isn't actively monitoring — disabled (``status==1``) or in
    maintenance (``maintenance_status==1``) — can't be trusted to report real
    reachability; its ``available`` is stale, so we return ``unknown`` rather
    than reading a leftover "up". Otherwise a host we deliberately disabled
    (e.g. a deactivated device) would surface to customers as healthy.
    """
    if str(zhost.get("status")) == "1":  # 0=enabled, 1=disabled
        return UNKNOWN
    if str(zhost.get("maintenance_status")) == "1":
        return UNKNOWN
    top = str(zhost.get("available") or "")
    if top == "1":
        return UP
    if top == "2":
        return DOWN
    ifaces = zhost.get("interfaces", []) or []
    main = next((i for i in ifaces if str(i.get("main")) == "1"), None)
    candidates = [main] if main else ifaces
    vals = {str(i.get("available")) for i in candidates if i}
    if "2" in vals and "1" not in vals:
        return DOWN
    if "1" in vals:
        return UP
    return UNKNOWN


def _derive(avail: str, icmp_up: bool | None = None) -> str:
    if icmp_up is True:
        return UP
    if icmp_up is False:
        return DOWN
    if avail == DOWN:
        return DOWN
    if avail == UP:
        return UP
    return UNKNOWN


def _uisp_status(value: str | None) -> str:
    """Map a UISP ``overview.status`` trapper value to up/down/unknown.

    The ``uisp.status`` trapper carries whatever UISP reports; be defensive
    about empty or unexpected values. Only ``active`` is healthy and
    ``disconnected``/``offline`` are down; everything else (``unauthorized``,
    ``unknown``, ``""``, or anything unforeseen) reads unknown.
    """
    v = (value or "").strip().lower()
    if v == "active":
        return UP
    if v in ("disconnected", "offline"):
        return DOWN
    return UNKNOWN


def warm_topology_status(session: Session, client) -> dict:
    """Refresh live_status for every reconciled, Zabbix-linked node."""
    nodes = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.source == SOURCE,
            NetworkDevice.zabbix_hostid.isnot(None),
            NetworkDevice.is_active.is_(True),
        )
        .all()
    )
    host_ids = [n.zabbix_hostid for n in nodes if n.zabbix_hostid]
    if not host_ids:
        return {"nodes": 0}

    avail: dict[str, str] = {}
    icmp_allowed: dict[str, bool] = {}
    icmp_up: dict[str, bool] = {}
    # UISP-observed status (hostid -> up/down/unknown), from the live
    # ``uisp.status`` trapper the importer pushes every cycle. Only trapper-only
    # UISP hosts (ONUs / non-ICMP stations) have this; it fills the gap where
    # there is no polled interface or icmpping, so those nodes are no longer
    # blindly "unknown". Polling always wins — see the per-node loop below.
    uisp_status: dict[str, str] = {}
    for chunk in _chunks(host_ids, _CHUNK):
        for h in client.get_hosts(host_ids=chunk):
            host_id = str(h.get("hostid"))
            avail[host_id] = _availability(h)
            icmp_allowed[host_id] = (
                str(h.get("status")) != "1" and str(h.get("maintenance_status")) != "1"
            )
        for item in client.get_items(host_ids=chunk, metric="icmpping", limit=100000):
            if str(item.get("key_") or "") != "icmpping":
                continue
            host_id = str(item.get("hostid"))
            value = str(item.get("lastvalue") or "")
            if value == "1" and host_id not in icmp_up:
                icmp_up[host_id] = True
            elif value == "0":
                icmp_up[host_id] = False
        for item in client.get_items(
            host_ids=chunk, metric="uisp.status", limit=100000
        ):
            if str(item.get("key_") or "") != "uisp.status":
                continue
            host_id = str(item.get("hostid"))
            uisp_status[host_id] = _uisp_status(item.get("lastvalue"))

    now = _now()
    sla_logging = _sla_log_enabled()
    coverage = _coverage() if sla_logging else None
    counts: Counter = Counter()
    via_uisp_status = 0  # nodes coloured from the uisp.status trapper fallback
    for n in nodes:
        hid = n.zabbix_hostid
        if hid is None:  # filtered in the query; narrows for the type checker
            continue
        raw_icmp = icmp_up.get(hid)
        zabbix_icmp = (
            raw_icmp if raw_icmp is False or icmp_allowed.get(hid, False) else None
        )
        status = _derive(avail.get(hid, UNKNOWN), zabbix_icmp)
        # Real Zabbix polling (interface availability + icmpping) is more
        # authoritative and real-time than the ~15-min UISP trapper, so it wins.
        # Only when the polled result is UNKNOWN — a trapper-only UISP host with
        # no polled interface or icmpping — do we fall back to its UISP-observed
        # status. A host with both keeps its icmp/interface result untouched.
        if status == UNKNOWN:
            fallback = uisp_status.get(hid)
            if fallback in (UP, DOWN):
                status = fallback
                via_uisp_status += 1
        # Stamp live_status_at only when the state CHANGES, so it marks when the
        # node entered its current state — the dwell clock the customer-facing
        # connection-status debounce relies on (see topology.selfcare).
        if n.live_status != status:
            # Bridge the transition into an uptime Alert interval so the SLA
            # report has real downtime to merge (flag-gated, additive — never
            # alters live_status). Skip devices with no monitoring path: their
            # "down" is a blind spot, not real downtime (Phase 3). See
            # availability_log / monitoring_coverage / INFRASTRUCTURE_SLA.
            if sla_logging and (
                coverage is None or coverage.covers(getattr(n, "mgmt_ip", None))
            ):
                from app.services.topology.availability_log import record_transition

                record_transition(session, n, status, now=now)
            n.live_status = status
            n.live_status_at = now
        counts[status] += 1
    session.flush()
    return {"nodes": len(nodes), **counts, "via_uisp_status": via_uisp_status}
