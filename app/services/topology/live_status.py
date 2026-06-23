"""Topology live-status warmer (Phase 3).

Batch-fetches Zabbix host availability + active triggers for the reconciled
nodes and writes a coarse ``live_status`` (up/down/problem/unknown) into the
network_devices cache. The Network Path panel reads that cache — Zabbix is
NEVER called on the request path (same warm-and-store pattern as
``monitoring_warm``). Severity order: down > problem > up.
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


def _now() -> datetime:
    return datetime.now(UTC)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _availability(zhost: dict) -> str:
    """Map Zabbix availability to up/down/unknown.

    Prefers an explicit host-level ``available`` (1 up, 2 down); falls back to
    interface availability (main interface first) for Zabbix 6+ where host-level
    availability was removed.
    """
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


def _derive(avail: str, has_problem: bool) -> str:
    if avail == DOWN:
        return DOWN
    if has_problem:
        return PROBLEM
    if avail == UP:
        return UP
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
    problems: set[str] = set()
    for chunk in _chunks(host_ids, _CHUNK):
        for h in client.get_hosts(host_ids=chunk):
            avail[str(h.get("hostid"))] = _availability(h)
        for t in client.get_triggers(host_ids=chunk, active_only=True, limit=10000):
            for hh in t.get("hosts", []):
                problems.add(str(hh.get("hostid")))

    now = _now()
    counts: Counter = Counter()
    for n in nodes:
        hid = n.zabbix_hostid
        if hid is None:  # filtered in the query; narrows for the type checker
            continue
        status = _derive(avail.get(hid, UNKNOWN), hid in problems)
        # Stamp live_status_at only when the state CHANGES, so it marks when the
        # node entered its current state — the dwell clock the customer-facing
        # connection-status debounce relies on (see topology.selfcare).
        if n.live_status != status:
            n.live_status = status
            n.live_status_at = now
        counts[status] += 1
    session.flush()
    return {"nodes": len(nodes), **counts}
