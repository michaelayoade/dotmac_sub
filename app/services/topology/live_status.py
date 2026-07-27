"""Topology live-status warmer.

Derives a coarse ``live_status`` (up/down/unknown) for reconciled topology
nodes from the native poll columns the infrastructure poller maintains
(``last_ping_*`` / ``last_snmp_*``, see ``services.infrastructure_polling``)
and writes it into the network_devices cache. A failed ping is always the
primary outage signal; ping success is the primary healthy signal; SNMP
reachability is used only when there is no fresh ping result. The Network
Path panel reads that cache — no probe ever runs on the request path (same
warm-and-store pattern the retired monitoring warmers used).

Formerly this warmer batch-fetched Zabbix host availability for reconciled
(``source == zabbix_reconcile``) nodes; the derived statuses, heartbeat key
and SLA availability bridge are unchanged, but the data source moved to the
native poll columns and the population is now source-agnostic: every active
*pollable* device (same predicate as the poll sweep) gets a live_status,
however its row was created. Unpollable devices keep a NULL live_status so
surfaces with their own fallbacks (e.g. linked-router status) still apply
them. The old ``uisp.status`` trapper fallback is gone: radio/CPE health
feeds the outage pipeline natively via ``CPEDevice.last_uisp_status``
(uisp_sync), and a pollable node with neither a fresh ping nor SNMP result
reads ``unknown``, which every consumer already treats conservatively.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.infrastructure_polling import pollable_device_criteria

UP = "up"
DOWN = "down"
PROBLEM = "problem"
UNKNOWN = "unknown"

# A poll result older than this no longer proves anything about the device —
# the poller has stopped covering it (disabled checks, poller down), so the
# node degrades to unknown instead of freezing on its last state. Generous
# multiple of the default 60s ping staleness window.
STALE_POLL_AFTER_SECONDS = 900

# Heartbeat written on every warm run so the customer-facing connection-status
# readers can tell whether live_status is being refreshed at all. If the warmer
# dies this key ages out and positive states stop being trusted — that is the
# warmer's dead-man switch, read through
# ``device_operational_status.warmer_is_stale`` and applied by
# :func:`trusted_live_status` below. TTL is far longer than the staleness window
# so the timestamp survives to be age-compared; a missing key reads as stale
# (fail closed), never as fresh.
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


def _fresh(checked_at: datetime | None, now: datetime, window_seconds: int) -> bool:
    if checked_at is None:
        return False
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return (now - checked_at).total_seconds() <= window_seconds


def derive_live_status(
    node: NetworkDevice,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> str:
    """Map a node's native poll columns to up/down/unknown.

    A device in operator ``maintenance`` can't be trusted to report real
    reachability (mirrors the old Zabbix maintenance handling): it reads
    ``unknown`` rather than surfacing a deliberate shutdown to customers as an
    outage. Ping is authoritative when fresh; SNMP reachability only fills in
    for ping-disabled devices.
    """
    now = now or _now()
    if node.status == DeviceStatus.maintenance:
        return UNKNOWN
    if (
        node.ping_enabled
        and node.last_ping_ok is not None
        and _fresh(node.last_ping_at, now, stale_after_seconds)
    ):
        return UP if node.last_ping_ok else DOWN
    if (
        node.snmp_enabled
        and node.last_snmp_ok is not None
        and _fresh(node.last_snmp_at, now, stale_after_seconds)
    ):
        return UP if node.last_snmp_ok else DOWN
    return UNKNOWN


def live_status_observed_at(
    node: NetworkDevice,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> datetime | None:
    """Return the native timestamp relevant to the current ``live_status``.

    ``live_status_at`` is deliberately not an observation timestamp: it records
    when the derived status last changed for debounce and availability dwell
    calculations. Freshness decisions must follow the same ping/SNMP precedence
    as :func:`derive_live_status` and use the selected collector timestamp. If
    no collector result is fresh enough to produce a conclusive status, return
    the preferred stale timestamp so callers can distinguish expired evidence
    from a legacy row that has no per-device observation evidence.
    """
    now = now or _now()
    if getattr(node, "status", None) == DeviceStatus.maintenance:
        return None
    if (
        getattr(node, "ping_enabled", False)
        and getattr(node, "last_ping_ok", None) is not None
        and _fresh(getattr(node, "last_ping_at", None), now, stale_after_seconds)
    ):
        return getattr(node, "last_ping_at", None)
    if (
        getattr(node, "snmp_enabled", False)
        and getattr(node, "last_snmp_ok", None) is not None
        and _fresh(getattr(node, "last_snmp_at", None), now, stale_after_seconds)
    ):
        return getattr(node, "last_snmp_at", None)
    if (
        getattr(node, "ping_enabled", False)
        and getattr(node, "last_ping_ok", None) is not None
    ):
        return getattr(node, "last_ping_at", None)
    if (
        getattr(node, "snmp_enabled", False)
        and getattr(node, "last_snmp_ok", None) is not None
    ):
        return getattr(node, "last_snmp_at", None)
    return None


def _is_pollable(node) -> bool:
    """Python mirror of :func:`pollable_device_criteria` for a loaded row.

    A node outside this set is never visited by the warmer, so whatever
    ``live_status`` it carries is frozen and can only get older.
    """
    if not getattr(node, "is_active", True):
        return False
    if not (
        getattr(node, "ping_enabled", False) or getattr(node, "snmp_enabled", False)
    ):
        return False
    return bool(getattr(node, "mgmt_ip", None) or getattr(node, "hostname", None))


def trusted_live_status(
    node,
    *,
    now: datetime | None = None,
    warm_stale: bool | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> str:
    """``live_status`` with the freshness gate applied — the read-side contract.

    ``live_status`` is a warmed cache. Nothing in the schema stops it from going
    stale, and before this gate existed a node that left the pollable set (or a
    dead warmer) kept whatever value it last held, forever. A frozen ``up`` is
    the dangerous case: ``health_classifier.classify_node`` treats mgmt ``up``
    with zero online customers as ``service_fault`` ("reachable, serving nobody
    — NOT an area outage"), so a frozen ``up`` silently vetoes outage detection
    and the customer surface reports the cabinet healthy indefinitely.

    The gate is therefore **asymmetric, deliberately**:

    * a positive assertion (``up``) requires live support — the row must be
      active, still pollable, its observation fresh, and the warmer alive.
      Otherwise it decays to ``unknown``;
    * a negative assertion (``down``) is left alone. It fails safe: it opens an
      incident an operator can see and close. Decaying it too would let a dead
      warmer *suppress* real outages, trading a silent false-healthy for a
      silent false-healthy.

    Anything that is not ``up`` passes through unchanged.
    """
    value = str(getattr(node, "live_status", None) or "").strip().lower()
    if value != UP:
        return value or UNKNOWN
    if not getattr(node, "is_active", True):
        return UNKNOWN
    if not _is_pollable(node):
        return UNKNOWN
    current = now or _now()
    observed = live_status_observed_at(
        node,
        now=current,
        stale_after_seconds=stale_after_seconds,
    )
    if observed is not None:
        return UP if _fresh(observed, current, stale_after_seconds) else UNKNOWN
    # No per-node observation timestamp at all: fall back to the warmer's global
    # dead-man switch rather than trusting an unsupported positive.
    if warm_stale is None:
        from app.services.device_operational_status import warmer_is_stale

        warm_stale = warmer_is_stale(current)
    return UNKNOWN if warm_stale else UP


def _decay_unwarmed_nodes(session: Session, warmed_ids: set, *, now: datetime) -> int:
    """Decay every node the warmer no longer visits to ``unknown``.

    The missing half of the warm loop. A device that leaves the pollable set —
    soft-deleted, checks disabled, management address removed — is simply not
    selected any more, so its last ``live_status`` was frozen in place with
    nothing to expire it. This pass is the repair: it converges those rows to
    ``unknown`` on the next warm, including rows already frozen in production
    before this fix shipped.

    Idempotent: rows already ``unknown``/NULL are skipped.
    """
    stale_rows = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.live_status.isnot(None),
            NetworkDevice.live_status != UNKNOWN,
        )
        .all()
    )
    decayed = 0
    for node in stale_rows:
        if node.id in warmed_ids:
            continue
        node.live_status = UNKNOWN
        node.live_status_at = now
        decayed += 1
    return decayed


def warm_topology_status(
    session: Session,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = STALE_POLL_AFTER_SECONDS,
) -> dict:
    """Refresh live_status for every active pollable device, and decay the rest.

    Two halves, both required: pollable nodes get a freshly derived state, and
    every other node carrying a stale state is decayed to ``unknown`` so no row
    can keep asserting reachability nothing is checking.
    """
    nodes = session.query(NetworkDevice).filter(*pollable_device_criteria()).all()
    now = now or _now()
    if not nodes:
        decayed = _decay_unwarmed_nodes(session, set(), now=now)
        if decayed:
            session.flush()
        return {"nodes": 0, "decayed": decayed}

    sla_logging = _sla_log_enabled()
    coverage = _coverage() if sla_logging else None
    counts: Counter = Counter()
    for n in nodes:
        status = derive_live_status(n, now=now, stale_after_seconds=stale_after_seconds)
        # Stamp live_status_at only when the state CHANGES, so it marks when the
        # node entered its current state — the dwell clock the customer-facing
        # connection-status debounce relies on (see topology.selfcare).
        if n.live_status != status:
            # Bridge the transition into an uptime Alert interval so the SLA
            # report has real downtime to merge (flag-gated, additive — never
            # alters live_status). Skip devices with no monitoring path: their
            # "down" is a blind spot, not real downtime. See
            # availability_log / monitoring_coverage / INFRASTRUCTURE_SLA.
            if sla_logging and (
                coverage is None or coverage.covers(getattr(n, "mgmt_ip", None))
            ):
                from app.services.topology.availability_log import record_transition

                record_transition(session, n, status, now=now)
            n.live_status = status
            n.live_status_at = now
        counts[status] += 1
    decayed = _decay_unwarmed_nodes(session, {n.id for n in nodes}, now=now)
    session.flush()
    return {"nodes": len(nodes), "decayed": decayed, **counts}
