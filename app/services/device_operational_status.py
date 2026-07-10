"""Derived device operational status — the NOC-facing truth (Phase 1).

`device.status` is administrative/lifecycle *intent*; `live_status` is the raw
monitoring *observation*. Neither alone is what an operator should read off the
Network Devices page: admin status goes stale, and raw live_status turns
monitoring gaps (no warm, stale warmer, no path) into fake outages.

`operational_status` is a derived *projection* over both — computed on read,
never persisted. See docs/designs/DEVICE_OPERATIONAL_STATUS.md.

Precedence (first match wins):
    admin maintenance/decommissioned  -> maintenance   (intentional; never alarm)
    no live_status row                -> unmonitored    (reason: not_warmed)
    warmer heartbeat stale            -> unmonitored    (reason: stale)
    live_status == unknown            -> unmonitored    (reason: monitoring_unknown)
    live_status == problem            -> degraded      (legacy cached status)
    live_status == down               -> down
    live_status == up                 -> up
    else                              -> unknown

Phase 1 scope: warmer-fed `live_status` + warmer-heartbeat freshness +
lifecycle override. Per-type ACS/OLT-poll sources and cached VPN-path coverage
(the real ``no_path`` distinction) are Phase 2/3 — until then a no-path device
reads ``unmonitored(monitoring_unknown)`` (it warms to ``unknown``), not
``down``, which already removes the false-outage trap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Derived UI buckets (keep small — reasons live in `reason`, not extra pills).
UP = "up"
DEGRADED = "degraded"
DOWN = "down"
UNMONITORED = "unmonitored"
MAINTENANCE = "maintenance"
UNKNOWN = "unknown"

# Reuse the customer-facing warmer staleness threshold (selfcare uses the same).
_WARM_STALE_SECONDS = 600


@dataclass(frozen=True)
class OperationalStatus:
    status: str  # one of the UI buckets above
    reason: str  # machine reason, surfaced in the tooltip / used as a filter
    admin_status: str | None  # the raw lifecycle intent, for the secondary text
    mismatch: bool  # admin intent conflicts with observed reality
    mismatch_reason: str | None

    @property
    def label(self) -> str:
        return {
            UP: "Up",
            DEGRADED: "Degraded",
            DOWN: "Down",
            UNMONITORED: "Unmonitored",
            MAINTENANCE: "Maintenance",
            UNKNOWN: "Unknown",
        }.get(self.status, self.status.title())

    @property
    def alarming(self) -> bool:
        """Only monitored down/degraded should drive alarms — never unmonitored."""
        return self.status in (DOWN, DEGRADED)


def warmer_is_stale(now: datetime | None = None) -> bool:
    """True only when the warmer heartbeat is present but older than the
    staleness window. A *missing* heartbeat is NOT stale (transient Redis hiccup
    shouldn't blank every device) — same rule as topology.selfcare."""
    try:
        from app.services.app_cache import get_json
        from app.services.topology.live_status import WARM_HEARTBEAT_KEY

        raw = get_json(WARM_HEARTBEAT_KEY)
    except Exception:
        return False
    if not raw:
        return False
    try:
        warmed_at = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if warmed_at.tzinfo is None:
        warmed_at = warmed_at.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - warmed_at) > timedelta(seconds=_WARM_STALE_SECONDS)


def _enum_value(v) -> str | None:
    if v is None:
        return None
    return getattr(v, "value", v)


def _is_fresh(ts, now: datetime, seconds: int) -> bool:
    """True if timestamp ``ts`` is within ``seconds`` of ``now`` (tz-safe)."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts) <= timedelta(seconds=seconds)


# ── Per-type derivers (Phase 2b) ─────────────────────────────────────────────
# OLT and ONT carry their own native liveness fields; unlike NetworkDevice they
# are NOT warmed into `live_status`. Today both tabs render an admin-ish
# value (OLT `runtime_status` = linked device admin status; ONT badge = is_active),
# so deriving from the real telemetry is an accuracy fix, not a re-skin.

# An ONT that informed ACS / was last seen within this window counts as reachable
# even if the OLT last reported it offline (multi-source "reachable if any").
_ONT_FRESH_SECONDS = 1800  # 30 min (~6 missed 5-min ACS informs)
# An OLT poll/ping older than this is no longer trustworthy.
_OLT_FRESH_SECONDS = 3600  # 1 hour


def _live_status_to_operational(live: str | None, reason_suffix: str):
    """Map a warmer-fed live_status string to an operational bucket."""
    if live == "up":
        return OperationalStatus(UP, f"observed_up{reason_suffix}", None, False, None)
    if live == "down":
        return OperationalStatus(
            DOWN, f"observed_down{reason_suffix}", None, False, None
        )
    if live == "problem":
        return OperationalStatus(
            DEGRADED, f"active_trigger{reason_suffix}", None, False, None
        )
    return None


def derive_olt_operational_status(
    olt,
    *,
    linked_live_status: str | None = None,
    warm_stale: bool = False,
    now: datetime | None = None,
):
    """Operational status for an OLT — direct ping/poll telemetry first, then a
    fall-back to the linked monitored device's live_status (reachable if any
    source).

    up   = pinged OK and last poll succeeded
    degraded = pinged OK but SNMP/poll failing (reachable, partial telemetry)
    down = ping failed
    unmonitored = no fresh telemetry from *either* source

    The fall-back matters in practice: OLT direct polling can be stale/dead while
    the OLT is still observed via its linked NetworkDevice.
    """
    now = now or datetime.now(UTC)
    last_ping_ok = getattr(olt, "last_ping_ok", None)
    last_ping_at = getattr(olt, "last_ping_at", None)
    poll = _enum_value(getattr(olt, "last_poll_status", None))

    direct_fresh = _is_fresh(last_ping_at, now, _OLT_FRESH_SECONDS)
    if direct_fresh and last_ping_ok is True:
        if poll == "success":
            return OperationalStatus(UP, "observed_up", None, False, None)
        return OperationalStatus(DEGRADED, f"poll_{poll or 'none'}", None, False, None)
    if direct_fresh and last_ping_ok is False:
        return OperationalStatus(DOWN, "ping_failed", None, False, None)

    # Direct telemetry missing/stale — fall back to the linked observation.
    if linked_live_status and not warm_stale:
        mapped = _live_status_to_operational(linked_live_status, "_linked")
        if mapped is not None:
            return mapped

    if last_ping_ok is None and last_ping_at is None:
        return OperationalStatus(UNMONITORED, "not_warmed", None, False, None)
    return OperationalStatus(UNMONITORED, "stale", None, False, None)


def derive_ont_operational_status(ont, *, now: datetime | None = None):
    """Operational status for an ONT, reconciling the OLT-reported state with
    ACS informs and last-seen — reachable if *any* source confirms recently.

    This closes the real gap: an ONT the OLT last reported ``offline`` but which
    informed ACS minutes ago is actually up. A never-seen ONT (no OLT-online, no
    ACS, no last-seen) is ``unmonitored``, not ``down``.
    """
    now = now or datetime.now(UTC)
    olt_status = _enum_value(getattr(ont, "olt_status", None))
    acs_fresh = _is_fresh(
        getattr(ont, "acs_last_inform_at", None), now, _ONT_FRESH_SECONDS
    )
    seen_fresh = _is_fresh(getattr(ont, "last_seen_at", None), now, _ONT_FRESH_SECONDS)

    if olt_status == "online":
        return OperationalStatus(UP, "olt_online", None, False, None)
    if acs_fresh:
        return OperationalStatus(UP, "acs_inform_recent", None, False, None)
    if seen_fresh:
        return OperationalStatus(UP, "seen_recent", None, False, None)
    # Not confirmed up by any source.
    ever_seen = (
        getattr(ont, "last_seen_at", None) is not None
        or getattr(ont, "acs_last_inform_at", None) is not None
    )
    if olt_status == "offline" and ever_seen:
        reason = _enum_value(getattr(ont, "offline_reason", None)) or "observed_down"
        return OperationalStatus(DOWN, reason, None, False, None)
    # Never seen by any source -> not monitored rather than a false "down".
    return OperationalStatus(UNMONITORED, "never_seen", None, False, None)


# Admin lifecycle states that are intentional and must override observation
# (we don't alarm on a device we deliberately took out of service).
_LIFECYCLE_OVERRIDE = {"maintenance", "decommissioned", "retired"}


def derive_operational_status(
    device, *, warm_stale: bool, coverage=None
) -> OperationalStatus:
    """Derive the operational status for one device-like object.

    ``device`` needs ``status`` / ``live_status`` (and ``mgmt_ip`` when coverage
    is supplied), read defensively. ``warm_stale`` is computed once per request.
    ``coverage`` is an optional MonitoringCoverage (Phase 3): when loaded, a
    device whose mgmt IP no live tunnel reaches reads ``unmonitored(no_path)``
    rather than a false ``down`` — *unless* it is observed ``up`` (a positive
    reading proves a path exists). Omitted/unloaded coverage = Phase-1 behaviour.
    """
    admin = _enum_value(getattr(device, "status", None))
    live = _enum_value(getattr(device, "live_status", None))

    # 1. Lifecycle intent wins — intentional states never become alarms.
    if admin in _LIFECYCLE_OVERRIDE:
        return OperationalStatus(MAINTENANCE, f"admin_{admin}", admin, False, None)

    # 2. No monitoring path (and not positively observed up) -> blind spot, not
    # an outage. Positive 'up' wins (it proves a path), so only gate non-up.
    if (
        coverage is not None
        and getattr(coverage, "loaded", False)
        and live != "up"
        and not coverage.covers(getattr(device, "mgmt_ip", None))
    ):
        return _maybe_mismatch(UNMONITORED, "no_path", admin)

    # 3/4/5. No trustworthy live observation -> unmonitored (distinct from down).
    if live is None:
        return _maybe_mismatch(UNMONITORED, "not_warmed", admin)
    if warm_stale:
        return _maybe_mismatch(UNMONITORED, "stale", admin)
    if live == "unknown":
        # Disabled / in-maintenance in monitoring, or no availability data.
        return _maybe_mismatch(UNMONITORED, "monitoring_unknown", admin)

    # 5. Live observation maps to the UI bucket. "problem" is kept for legacy
    # cached rows from the older warmer that folded active triggers into status.
    if live == "problem":
        return _maybe_mismatch(DEGRADED, "active_trigger", admin)
    if live == "down":
        return _maybe_mismatch(DOWN, "observed_down", admin)
    if live == "up":
        return _maybe_mismatch(UP, "observed_up", admin)
    return _maybe_mismatch(UNKNOWN, "indeterminate", admin)


def _maybe_mismatch(status: str, reason: str, admin: str | None) -> OperationalStatus:
    """Flag inventory-hygiene conflicts between admin intent and observation."""
    mismatch = False
    mreason: str | None = None
    if admin == "online" and status in (DOWN, DEGRADED):
        mismatch, mreason = True, "admin_online_observed_down"
    elif admin == "offline" and status in (UP, DEGRADED):
        mismatch, mreason = True, "admin_offline_observed_up"
    elif admin in ("online", "offline") and status == UNMONITORED:
        mismatch, mreason = True, "active_but_unmonitored"
    return OperationalStatus(status, reason, admin, mismatch, mreason)


# Mismatch reason -> (operator-facing label, owning team). The worklist groups
# by this so inventory-hygiene conflicts route to whoever can fix them.
_MISMATCH_OWNERS = {
    "admin_online_observed_down": (
        "Admin says online, monitoring sees down/degraded",
        "Field ops",
    ),
    "admin_offline_observed_up": (
        "Admin says offline, monitoring sees it up",
        "Inventory hygiene",
    ),
    "active_but_unmonitored": (
        "Active in inventory, but no monitoring coverage",
        "Net-eng / VPN",
    ),
}


def mismatch_worklist(db, *, reason: str | None = None) -> dict:
    """Devices whose admin intent conflicts with observed reality, grouped by
    reason and routed to an owning team. The operational hygiene queue.

    Read-only; derives operational status on the fly (no persisted state)."""
    from app.models.network_monitoring import NetworkDevice

    devices = list(
        db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    )
    annotate_operational_status(devices)

    groups: dict[str, dict] = {}
    for d in devices:
        op = getattr(d, "operational", None)
        if not op or not op.mismatch or not op.mismatch_reason:
            continue
        if reason and op.mismatch_reason != reason:
            continue
        label, owner = _MISMATCH_OWNERS.get(
            op.mismatch_reason, (op.mismatch_reason, "Unassigned")
        )
        g = groups.setdefault(
            op.mismatch_reason,
            {"reason": op.mismatch_reason, "label": label, "owner": owner, "rows": []},
        )
        g["rows"].append(
            {
                "id": d.id,
                "name": d.name,
                "mgmt_ip": getattr(d, "mgmt_ip", None),
                "admin": op.admin_status,
                "operational": op.status,
                "operational_label": op.label,
            }
        )

    ordered = sorted(groups.values(), key=lambda g: len(g["rows"]), reverse=True)
    for g in ordered:
        g["count"] = len(g["rows"])
        g["rows"].sort(key=lambda r: (r["name"] or "").lower())
    return {
        "groups": ordered,
        "total": sum(g["count"] for g in ordered),
        "reason_filter": reason,
        "reasons": list(_MISMATCH_OWNERS),
    }


def annotate_operational_status(devices, *, now: datetime | None = None) -> None:
    """Attach a transient ``.operational`` to each device for templates.

    Computes warmer staleness + monitoring coverage once for the whole batch.
    Safe on ORM instances and on stub objects (attributes read defensively).
    """
    warm_stale = warmer_is_stale(now)
    try:
        from app.services.monitoring_coverage import get_coverage

        coverage = get_coverage()
    except Exception:
        coverage = None
    for device in devices:
        try:
            device.operational = derive_operational_status(
                device, warm_stale=warm_stale, coverage=coverage
            )
        except Exception:
            # Never let status derivation break a page render.
            device.operational = OperationalStatus(
                UNKNOWN,
                "error",
                _enum_value(getattr(device, "status", None)),
                False,
                None,
            )
