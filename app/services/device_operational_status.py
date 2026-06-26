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
    live_status == problem            -> degraded
    live_status == down               -> down
    live_status == up                 -> up
    else                              -> unknown

Phase 1 scope: Zabbix-warmed `live_status` + warmer-heartbeat freshness +
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


# Admin lifecycle states that are intentional and must override observation
# (we don't alarm on a device we deliberately took out of service).
_LIFECYCLE_OVERRIDE = {"maintenance", "decommissioned", "retired"}


def derive_operational_status(device, *, warm_stale: bool) -> OperationalStatus:
    """Derive the operational status for one device-like object.

    ``device`` only needs ``status``, ``live_status`` attributes (read
    defensively). ``warm_stale`` is computed once per request via
    ``warmer_is_stale`` and passed in, so this stays a pure function.
    """
    admin = _enum_value(getattr(device, "status", None))
    live = _enum_value(getattr(device, "live_status", None))

    # 1. Lifecycle intent wins — intentional states never become alarms.
    if admin in _LIFECYCLE_OVERRIDE:
        return OperationalStatus(MAINTENANCE, f"admin_{admin}", admin, False, None)

    # 2/3/4. No trustworthy live observation -> unmonitored (distinct from down).
    if live is None:
        return _maybe_mismatch(UNMONITORED, "not_warmed", admin)
    if warm_stale:
        return _maybe_mismatch(UNMONITORED, "stale", admin)
    if live == "unknown":
        # Disabled / in-maintenance in Zabbix, or no availability data (incl. the
        # no-path blind spot until the Phase 3 coverage job lands).
        return _maybe_mismatch(UNMONITORED, "monitoring_unknown", admin)

    # 5. Live observation maps to the UI bucket. problem == up-with-trigger.
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


def annotate_operational_status(devices, *, now: datetime | None = None) -> None:
    """Attach a transient ``.operational`` to each device for templates.

    Computes warmer staleness once for the whole batch. Safe on ORM instances
    and on stub objects (attributes read defensively).
    """
    warm_stale = warmer_is_stale(now)
    for device in devices:
        try:
            device.operational = derive_operational_status(
                device, warm_stale=warm_stale
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
