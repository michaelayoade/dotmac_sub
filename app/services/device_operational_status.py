"""Derived device operational status — the NOC-facing truth (Phase 1).

`device.status` is administrative/lifecycle *intent*; `live_status` is the raw
monitoring *observation*. Neither alone is what an operator should read off the
Network Devices page: admin status goes stale, and raw live_status turns
monitoring gaps (no warm, stale warmer, no path) into fake outages.

`operational_status` is a derived *projection* over both — computed on read,
never persisted. See docs/designs/DEVICE_OPERATIONAL_STATUS.md.

Precedence (first match wins):
    admin maintenance/decommissioned  -> maintenance   (intentional; never alarm)
    no live_status row                -> down           (reason: not_warmed_retry_pending)
    warmer heartbeat stale            -> retain state   (reason: stale_retry_pending)
    live_status == unknown            -> down           (reason: monitoring_unknown_retry_pending)
    live_status == problem            -> degraded      (legacy cached status)
    live_status == down               -> down
    live_status == up                 -> up
    else                              -> unknown

Phase 1 scope: warmer-fed `live_status` + warmer-heartbeat freshness +
lifecycle override. Per-type ACS/OLT-poll sources and cached VPN-path coverage
(the real ``no_path`` distinction) are Phase 2/3 — until then a no-path device
reads ``down(monitoring_unknown_retry_pending)`` while the poller retries. A
retry-pending state is binary for operators but does not alarm without negative
device evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


# Derived UI buckets (keep small — reasons live in `reason`, not extra pills).
class DeviceOperationalState(StrEnum):
    """Authoritative vocabulary for the derived NOC-facing projection."""

    up = "up"
    degraded = "degraded"
    down = "down"
    maintenance = "maintenance"


DEVICE_OPERATIONAL_STATE_VALUES = tuple(state.value for state in DeviceOperationalState)

# Compatibility aliases for existing decision and policy callers.
UP = DeviceOperationalState.up.value
DEGRADED = DeviceOperationalState.degraded.value
DOWN = DeviceOperationalState.down.value
MAINTENANCE = DeviceOperationalState.maintenance.value

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
    def presentation(self):
        """Return the shared cross-client semantic presentation contract."""
        from app.services.status_presentation import (
            device_operational_status_presentation,
        )

        return device_operational_status_presentation(self)

    @property
    def label(self) -> str:
        """Compatibility accessor; semantic labels are owned by presentation."""
        return self.presentation.label

    @property
    def alarming(self) -> bool:
        """Retry gaps are visible but do not become outages without evidence."""
        return self.status in (DOWN, DEGRADED) and not self.retry_pending

    @property
    def retry_pending(self) -> bool:
        return self.reason.endswith("_retry_pending")


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
    down + retry_pending = no fresh telemetry from either source

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
        return OperationalStatus(DOWN, "not_warmed_retry_pending", None, False, None)
    retained = UP if last_ping_ok is True else DOWN
    return OperationalStatus(retained, "stale_retry_pending", None, False, None)


def derive_ont_operational_status(ont, *, now: datetime | None = None):
    """Operational status for an ONT, reconciling the OLT-reported state with
    ACS informs and last-seen — reachable if *any* source confirms recently.

    This closes the real gap: an ONT the OLT last reported ``offline`` but which
    informed ACS minutes ago is actually up. A never-seen ONT (no OLT-online, no
    ACS, no last-seen) is ``offline`` with a retry pending.
    """
    from app.services.network.ont_status import resolve_effective_ont_status

    effective = resolve_effective_ont_status(ont, now=now)
    return OperationalStatus(
        UP if effective.is_online else DOWN,
        effective.reason,
        None,
        False,
        None,
    )


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
    device whose mgmt IP no live tunnel reaches retains its last binary state
    with a retry-pending reason. Omitted/unloaded coverage = Phase-1 behaviour.
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
        retained = DOWN if live != "up" else UP
        return _maybe_mismatch(retained, "no_path_retry_pending", admin)

    # 3/4/5. No trustworthy observation remains binary and schedules retries.
    if live is None:
        return _maybe_mismatch(DOWN, "not_warmed_retry_pending", admin)
    if warm_stale:
        retained = UP if live == "up" else DOWN
        return _maybe_mismatch(retained, "stale_retry_pending", admin)
    if live == "unknown":
        # Disabled / in-maintenance in monitoring, or no availability data.
        return _maybe_mismatch(DOWN, "monitoring_unknown_retry_pending", admin)

    # 5. Live observation maps to the UI bucket. "problem" is kept for legacy
    # cached rows from the older warmer that folded active triggers into status.
    if live == "problem":
        return _maybe_mismatch(DEGRADED, "active_trigger", admin)
    if live == "down":
        return _maybe_mismatch(DOWN, "observed_down", admin)
    if live == "up":
        return _maybe_mismatch(UP, "observed_up", admin)
    return _maybe_mismatch(DOWN, "indeterminate_retry_pending", admin)


def _maybe_mismatch(status: str, reason: str, admin: str | None) -> OperationalStatus:
    """Flag inventory-hygiene conflicts between admin intent and observation."""
    mismatch = False
    mreason: str | None = None
    if admin in ("online", "offline") and reason.endswith("_retry_pending"):
        mismatch, mreason = True, "active_retry_pending"
    elif admin == "online" and status in (DOWN, DEGRADED):
        mismatch, mreason = True, "admin_online_observed_down"
    elif admin == "offline" and status in (UP, DEGRADED):
        mismatch, mreason = True, "admin_offline_observed_up"
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
    "active_retry_pending": (
        "Active in inventory, awaiting monitoring confirmation",
        "Net-eng / VPN",
    ),
}


def mismatch_worklist(
    db,
    *,
    reason: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict:
    """Devices whose admin intent conflicts with observed reality, grouped by
    reason and routed to an owning team. The operational hygiene queue.

    Read-only; derives operational status on the fly (no persisted state)."""
    from app.models.network_monitoring import NetworkDevice

    devices = list(
        db.query(NetworkDevice).filter(NetworkDevice.is_active.is_(True)).all()
    )
    annotate_operational_status(devices)

    search_filter = (search or "").strip()
    search_match = search_filter.lower()
    groups: dict[str, dict] = {}
    for d in devices:
        op = getattr(d, "operational", None)
        if not op or not op.mismatch or not op.mismatch_reason:
            continue
        if reason and op.mismatch_reason != reason:
            continue
        if search_match and search_match not in " ".join(
            str(value or "").lower()
            for value in (
                d.name,
                getattr(d, "mgmt_ip", None),
                op.admin_status,
                op.status,
                op.label,
                op.mismatch_reason,
            )
        ):
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
                "status_presentation": op.presentation,
            }
        )

    ordered = sorted(groups.values(), key=lambda g: len(g["rows"]), reverse=True)
    for g in ordered:
        g["count"] = len(g["rows"])
        g["rows"].sort(key=lambda r: (r["name"] or "").lower())

    total = sum(g["count"] for g in ordered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    end = start + per_page
    flattened = [(group, row) for group in ordered for row in group["rows"]]
    page_groups: list[dict] = []
    page_group_by_reason: dict[str, dict] = {}
    for source_group, row in flattened[start:end]:
        page_group = page_group_by_reason.get(source_group["reason"])
        if page_group is None:
            page_group = {
                key: value for key, value in source_group.items() if key != "rows"
            }
            page_group["rows"] = []
            page_group_by_reason[source_group["reason"]] = page_group
            page_groups.append(page_group)
        page_group["rows"].append(row)

    return {
        "groups": page_groups,
        "total": total,
        "reason_filter": reason,
        "search": search_filter,
        "reasons": list(_MISMATCH_OWNERS),
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    }


def annotate_operational_status(devices, *, now: datetime | None = None) -> None:
    """Attach the transient operational and presentation projections.

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
            operational = derive_operational_status(
                device, warm_stale=warm_stale, coverage=coverage
            )
        except Exception:
            # Never let status derivation break a page render.
            operational = OperationalStatus(
                DOWN,
                "derivation_error_retry_pending",
                _enum_value(getattr(device, "status", None)),
                False,
                None,
            )
        _attach_operational_projection(device, operational)


def _attach_operational_projection(device, operational: OperationalStatus) -> None:
    """Expose one derived state consistently to Jinja and API serializers."""
    device.operational = operational
    device.operational_status = operational.status
    device.operational_reason = operational.reason
    device.operational_retry_pending = operational.retry_pending
    device.status_presentation = operational.presentation
