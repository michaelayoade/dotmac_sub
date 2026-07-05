"""Outage auto-detection evaluator (Phase 5b).

TRANSITIONS, not absolute state: roughly a third of the fleet is chronically
offline from churn, so any absolute-state rule ("N devices are down") drowns
in noise. Each scan:

1. gathers what *newly went down* inside a lookback window;
2. suppresses victims that are merely unreachable behind a down parent
   (``reachability.classify_down_devices``) and attributes them to the
   topmost down ancestor — one root cause instead of N alerts;
3. groups the remaining victims by their shared parent scope (AP node for
   radios; basestation collapse for co-sited roots) and trips a scope only
   when BOTH thresholds pass: ``min_affected`` AND ``min_fraction`` of the
   scope's children that were up at the window start;
4. opens an OutageIncident through the existing manual declare path for each
   tripped scope not already covered by an open incident.

NO auto-resolve and NO customer notifications (Phase 5 deferrals stand);
idempotency across runs comes from the open-incident check.

Transition sources
------------------
- Infra devices: ``live_status == down AND live_status_at >= window start``.
  ``live_status_at`` is stamped by the warmer ONLY when the state changes, so
  it is a true transition timestamp. The availability_log uptime Alerts also
  record down transitions, but that bridge is flag-gated
  (``sla_availability_log_enabled``, default OFF), so it cannot be the
  primary signal here.
- Radios (wireless CPE): the DB has NO transition stamp — ``uisp_synced_at``
  is rewritten for every row on every sync and ``last_uisp_status`` has no
  changed-at column — so the scan keeps a per-radio up/down snapshot in the
  app cache and diffs it between runs. The baseline TTL equals the lookback
  window: a stalled scanner re-seeds instead of diffing ancient state.
- ONTs: skipped. ``OntUnit.olt_status_seen_at`` is an observation stamp
  rewritten on every poll (see ``ont_status.apply_olt_status_observation``),
  not a transition stamp, so there is no cheap "recently went offline"
  signal for the OLT/PON scope yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network import CPEDevice, DeviceStatus
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.affected import affected_customers
from app.services.topology.customer_path import resolve_upstream_chain
from app.services.topology.live_status import DOWN
from app.services.topology.outage import (
    AUTO_DETECT_ACTOR,
    AUTO_NOTE_PREFIX,
    declare_outage,
    open_incident_for_path,
)
from app.services.topology.reachability import (
    CLASS_UNREACHABLE_UPSTREAM,
    classify_down_devices,
)

logger = logging.getLogger(__name__)

# Settings (SettingDomain.network_monitoring) with their defaults.
LOOKBACK_MINUTES_DEFAULT = 15
MIN_AFFECTED_DEFAULT = 3
MIN_FRACTION_PCT_DEFAULT = 40  # stored as an integer percentage

RADIO_BASELINE_CACHE_KEY = "topology:outage_autodetect:radio_baseline"

# UISP radio statuses that count as "up" for transition detection. NULL covers
# rows written before the status column existed (same reading as customer_path
# / affected: only an explicit bad status is down).
_RADIO_UP_STATUSES = (None, "active")


def _now() -> datetime:
    return datetime.now(UTC)


def _setting_int(session: Session, key: str, default: int) -> int:
    try:
        from app.services.settings_spec import resolve_value

        raw = resolve_value(session, SettingDomain.network_monitoring, key)
        if raw is None:
            return default
        return int(str(raw))
    except Exception:  # settings are advisory; never fail the scan over them
        return default


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _coerce_uuid(value):
    """Snapshot keys round-trip through JSON as strings; the ORM wants UUIDs."""
    import uuid as _uuid

    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (TypeError, ValueError):
        return value


@dataclass
class _Candidate:
    """A tripped scope pending the open-incident check + declare.

    Duck-types the ``CustomerPath`` fields ``open_incident_for_path`` reads
    (node / upstream_chain / basestation), so the auto path reuses the exact
    matching semantics the customer-facing banner uses.
    """

    node: NetworkDevice | None = None
    basestation: PopSite | None = None
    upstream_chain: list[NetworkDevice] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def radio_snapshot(session: Session) -> dict:
    """Current per-radio up/down state: ``{cpe_id: {"ap": ap_id, "up": bool}}``.

    Only subscriber-linked, active, AP-parented, non-vanished radios — the
    same population the wireless impact arm counts.
    """
    snapshot: dict = {}
    for cpe_id, parent_id, uisp_status in (
        session.query(
            CPEDevice.id, CPEDevice.parent_network_device_id, CPEDevice.last_uisp_status
        )
        .filter(
            CPEDevice.parent_network_device_id.isnot(None),
            CPEDevice.subscriber_id.isnot(None),
            CPEDevice.status == DeviceStatus.active,
        )
        .all()
    ):
        if uisp_status == "vanished":
            continue
        snapshot[str(cpe_id)] = {
            "ap": str(parent_id),
            "up": uisp_status in _RADIO_UP_STATUSES,
        }
    return snapshot


def load_radio_baseline() -> dict | None:
    try:
        from app.services.app_cache import get_json

        baseline = get_json(RADIO_BASELINE_CACHE_KEY)
        return baseline if isinstance(baseline, dict) else None
    except Exception:  # cache is advisory
        return None


def store_radio_baseline(baseline: dict, *, ttl_seconds: int) -> None:
    try:
        from app.services.app_cache import set_json

        set_json(RADIO_BASELINE_CACHE_KEY, baseline, ttl_seconds)
    except Exception:  # cache is advisory; the next run just re-seeds
        logger.warning("outage_autodetect_baseline_store_failed", exc_info=True)


def _recent_down_devices(session: Session, window_start: datetime) -> list:
    """Active devices whose live_status turned down within the window.

    ``live_status_at`` marks when the device ENTERED its current state, so a
    chronically-down device (down before the window) is excluded here.
    """
    devices = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.live_status == DOWN,
            NetworkDevice.live_status_at.isnot(None),
        )
        .all()
    )
    return [
        d for d in devices if (_aware(d.live_status_at) or window_start) >= window_start
    ]


def evaluate_outages(
    session: Session,
    *,
    now: datetime | None = None,
    radio_baseline: dict | None = None,
) -> tuple[dict, dict]:
    """One auto-detection pass. Returns ``(counters, new_radio_baseline)``.

    ``radio_baseline`` is the previous run's ``radio_snapshot`` (None on the
    first run / after a cache miss — then the radio arm only seeds). Pure
    with respect to the cache; the task wrapper owns baseline persistence.
    """
    now = now or _now()
    lookback_minutes = max(
        _setting_int(
            session, "outage_autodetect_lookback_minutes", LOOKBACK_MINUTES_DEFAULT
        ),
        1,
    )
    min_affected = max(
        _setting_int(session, "outage_autodetect_min_affected", MIN_AFFECTED_DEFAULT), 1
    )
    min_fraction = (
        max(
            min(
                _setting_int(
                    session,
                    "outage_autodetect_min_fraction_pct",
                    MIN_FRACTION_PCT_DEFAULT,
                ),
                100,
            ),
            1,
        )
        / 100.0
    )
    window_start = now - timedelta(minutes=lookback_minutes)

    counters = {
        "events_seen": 0,
        "suppressed_unreachable": 0,
        "scopes_evaluated": 0,
        "incidents_created": 0,
        "skipped_open_incident": 0,
        "errors": 0,
    }

    # --- 1. transition events -------------------------------------------------
    recent_down = _recent_down_devices(session, window_start)
    classification = classify_down_devices(session)

    snapshot = radio_snapshot(session)
    radio_events_by_ap: dict[str, int] = {}
    baseline_up_by_ap: dict[str, int] = {}
    if radio_baseline:
        for cpe_id, previous in radio_baseline.items():
            if not isinstance(previous, dict) or not previous.get("up"):
                continue  # chronically-offline radios never become events
            ap_id = str(previous.get("ap") or "")
            if not ap_id:
                continue
            baseline_up_by_ap[ap_id] = baseline_up_by_ap.get(ap_id, 0) + 1
            current = snapshot.get(cpe_id)
            if current is not None and not current["up"]:
                radio_events_by_ap[ap_id] = radio_events_by_ap.get(ap_id, 0) + 1

    counters["events_seen"] = len(recent_down) + sum(radio_events_by_ap.values())

    # --- 2. suppress unreachable-behind-parent victims -------------------------
    root_events: list[NetworkDevice] = []
    for device in recent_down:
        info = classification.get(device.id)
        if info is not None and info.classification == CLASS_UNREACHABLE_UPSTREAM:
            counters["suppressed_unreachable"] += 1
            continue  # attributed to the topmost down ancestor, not itself
        root_events.append(device)

    # --- 3. tripped scopes -> candidates ---------------------------------------
    candidates: dict[str, _Candidate] = {}

    def _node_candidate(device: NetworkDevice, reason: str) -> None:
        key = f"node:{device.id}"
        if key in candidates:
            candidates[key].reasons.append(reason)
        else:
            candidates[key] = _Candidate(node=device, reasons=[reason])

    # 3a. AP scopes (radios), most specific. A scope trips when BOTH
    # thresholds pass; the denominator is the AP's radios that were up at the
    # window start (the baseline), so chronically-offline churn never inflates
    # or deflates the fraction. Defensive on tiny denominators: a zero or
    # sub-threshold population can never trip (min_affected floors it).
    for ap_id, victims in radio_events_by_ap.items():
        counters["scopes_evaluated"] += 1
        denominator = baseline_up_by_ap.get(ap_id, 0)
        if denominator <= 0 or victims < min_affected:
            continue
        if (victims / denominator) < min_fraction:
            continue
        try:
            ap = session.get(NetworkDevice, _coerce_uuid(ap_id))
            if ap is None:
                continue
            target = ap
            info = classification.get(ap.id)
            if info is not None and info.classification == CLASS_UNREACHABLE_UPSTREAM:
                # The AP itself is only unreachable — pin the incident on the
                # root cause instead of the symptom.
                root = session.get(NetworkDevice, info.root_cause_device_id)
                if root is not None:
                    target = root
            reason = (
                f"{victims}/{denominator} radios on AP {ap.name} went down "
                f"within {lookback_minutes}m"
            )
            if target.id != ap.id:
                reason += f" (root cause: {target.name})"
            _node_candidate(target, reason)
        except Exception:
            counters["errors"] += 1
            logger.exception("outage_autodetect_ap_scope_failed", extra={"ap": ap_id})

    # 3b. Infra root causes: a device that itself turned down with no down
    # ancestor. Zabbix already confirmed the failure, so no fraction gate —
    # but min_affected (on downstream subscriber impact) keeps devices that
    # serve (nearly) nobody from opening incidents.
    for device in root_events:
        counters["scopes_evaluated"] += 1
        try:
            impact = affected_customers(session, node=device)
            if impact["count"] < min_affected:
                continue
            _node_candidate(
                device,
                f"device {device.name} went down within {lookback_minutes}m "
                f"({impact['count']} subscriptions affected)",
            )
        except Exception:
            counters["errors"] += 1
            logger.exception(
                "outage_autodetect_device_scope_failed",
                extra={"device": str(device.id)},
            )

    # 3c. Basestation collapse: several distinct root causes at one pop_site is
    # a site problem (power/backhaul) — declare ONE basestation incident, not N.
    by_site: dict = {}
    for key, candidate in list(candidates.items()):
        site_id = getattr(candidate.node, "pop_site_id", None)
        if site_id is not None:
            by_site.setdefault(site_id, []).append(key)
    for site_id, keys in by_site.items():
        if len(keys) < 2:
            continue
        pop = session.get(PopSite, site_id)
        if pop is None:
            continue
        merged = _Candidate(basestation=pop)
        for key in keys:
            merged.reasons.extend(candidates.pop(key).reasons)
        merged.reasons.insert(0, f"{len(keys)} devices at {pop.name} tripped together")
        candidates[f"basestation:{site_id}"] = merged

    # --- 4. declare, skipping anything an open incident already covers ---------
    for key, candidate in candidates.items():
        try:
            if candidate.node is not None:
                candidate.upstream_chain = resolve_upstream_chain(
                    session, candidate.node
                )
                if candidate.node.pop_site_id is not None:
                    candidate.basestation = session.get(
                        PopSite, candidate.node.pop_site_id
                    )
            if open_incident_for_path(session, candidate) is not None:
                counters["skipped_open_incident"] += 1
                continue
            note = f"{AUTO_NOTE_PREFIX} " + "; ".join(candidate.reasons)
            declare_outage(
                session,
                node=candidate.node,
                basestation=candidate.basestation if candidate.node is None else None,
                declared_by=AUTO_DETECT_ACTOR,
                note=note[:2000],
            )
            counters["incidents_created"] += 1
        except Exception:
            counters["errors"] += 1
            logger.exception("outage_autodetect_declare_failed", extra={"scope": key})

    return counters, snapshot


def evaluate_with_cached_baseline(session: Session) -> tuple[dict, dict]:
    """Production entry point: evaluate against the cached radio baseline.

    Returns ``(counters, new_baseline)``; the caller persists the baseline
    AFTER committing, so a failed commit does not advance it (the next run
    re-detects the same transitions and the open-incident check dedupes).
    """
    return evaluate_outages(session, radio_baseline=load_radio_baseline())


def baseline_ttl_seconds(session: Session) -> int:
    """Baseline TTL == the lookback window, so a diff can never claim a
    transition older than the window after the scanner stalls."""
    lookback_minutes = max(
        _setting_int(
            session, "outage_autodetect_lookback_minutes", LOOKBACK_MINUTES_DEFAULT
        ),
        1,
    )
    return lookback_minutes * 60
