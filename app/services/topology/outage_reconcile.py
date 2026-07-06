"""Detected-outage incident reconcile (design §7.6).

The classifier (``health_classifier``) is a point-in-time read: it says what is
dark *right now*. That is too twitchy to notify on — a single missed poll or a
reboot would open and close an "outage" every few minutes. This module adds the
TIME dimension: a discover-reconcile loop that debounces the classifier's
verdicts into a trustworthy incident with a lifecycle, so notify / ticket /
restore / MTTR can later attach to a persisted spine instead of a transient read.

Each pass (mirrors the radius / outage-autodetect sweeps — advisory-lock
single-flight in the task wrapper, per-item savepoint here so one bad candidate
never poisons the run):

1. **Verdicts** — classify every active, zabbix-linked node with the online
   overlay (``affected.subscriptions_for_nodes`` + proof-of-life) and mgmt
   ``live_status``; keep the nodes currently ``node_outage``. Localize each dark
   node to the deepest dark-under-live boundary (``localize_outage``) -> a
   candidate outage per dark component (root_node, basestation, affected_count,
   confidence, classification).
2. **Identity** — find-or-open an OPEN classifier incident per candidate by the
   §7.6 identity rule (basestation, else exact root, else connected-dark
   component), RE-POINTING the root on localization drift rather than opening a
   duplicate.
3. **Debounce up** — ``suspected -> confirmed`` once the suspicion has persisted
   ``W_confirm(affected_count)`` (impact-scaled), else it stays suspected; a
   suspected incident whose node recovered before W_confirm is ``discarded``
   (the false-positive suppression — no confirmed event ever fires).
4. **Debounce down** — ``confirmed -> clearing`` on recovery (stamp cleared_at),
   ``clearing -> resolved`` once recovery is sustained past ``W_resolve``, and
   ``clearing -> confirmed`` (reopen) if it re-darkens inside the resolve window
   (hysteresis).

Firing stays GATED: this loop only persists the lifecycle and fans lifecycle
events into the existing webhook machinery. No customer / ticket notification is
sent here. MTTR is derivable as ``resolved_at - confirmed_at``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import NetworkDevice, OutageIncident
from app.services.topology.affected import (
    _dist_to_core,
    affected_customers,
    downstream_nodes,
    lldp_adjacency,
    subscriptions_for_nodes,
)
from app.services.topology.health_classifier import (
    NODE_OUTAGE,
    classify_node,
    localize_outage,
    online_subscription_ids,
)
from app.services.topology.outage import (
    CLASSIFIER_OPEN_STATUSES,
    CLASSIFIER_SOURCE,
    confirm_incident,
    discard_incident,
    find_open_classifier_incident,
    open_classifier_incident,
    reopen_incident,
    repoint_root,
    resolve_classifier_incident,
    start_clearing,
    update_classifier_snapshot,
)

logger = logging.getLogger(__name__)

# Single-flight guard (pg advisory lock via db_session_adapter.advisory_lock,
# same pattern as radius reconcile / outage_autodetect). "oir" = Outage Incident
# Reconcile — distinct from the autodetect scan's key so the two never contend.
ADVISORY_LOCK_KEY = 0x6F_69_72

# W_confirm (suspected -> confirmed) is scaled by impact (design §7.6 decision 2):
# a large blast radius is unambiguous and confirms immediately; a small one waits
# out flaps. All settings-backed (SettingDomain.network_monitoring) with these
# fallbacks.
CONFIRM_SECONDS_LARGE_DEFAULT = 0  # affected_count >= threshold_large -> now
CONFIRM_SECONDS_MED_DEFAULT = 360  # threshold_med .. threshold_large-1
CONFIRM_SECONDS_SMALL_DEFAULT = 600  # < threshold_med
CONFIRM_THRESHOLD_LARGE_DEFAULT = 20
CONFIRM_THRESHOLD_MED_DEFAULT = 5
# W_resolve (clearing -> resolved): sustained-recovery window, fixed default.
RESOLVE_SECONDS_DEFAULT = 300

# Coarse classifier confidence label -> stored Float (the model column is Float;
# localize_outage still returns the labelled band).
_CONFIDENCE_SCORE = {"low": 0.3, "medium": 0.6, "high": 0.9}


def _setting_int(session: Session, key: str, default: int) -> int:
    """Advisory settings read (never fails the pass over a bad setting)."""
    try:
        from app.services.settings_spec import resolve_value

        raw = resolve_value(session, SettingDomain.network_monitoring, key)
        if raw is None:
            return default
        return int(str(raw))
    except Exception:  # settings are advisory
        return default


@dataclass(frozen=True)
class _Windows:
    confirm: dict
    resolve: int


def _resolve_windows(session: Session) -> _Windows:
    small = max(
        _setting_int(
            session, "outage_confirm_seconds_small", CONFIRM_SECONDS_SMALL_DEFAULT
        ),
        0,
    )
    med = max(
        _setting_int(
            session, "outage_confirm_seconds_med", CONFIRM_SECONDS_MED_DEFAULT
        ),
        0,
    )
    large = max(
        _setting_int(
            session, "outage_confirm_seconds_large", CONFIRM_SECONDS_LARGE_DEFAULT
        ),
        0,
    )
    threshold_med = max(
        _setting_int(
            session, "outage_confirm_threshold_med", CONFIRM_THRESHOLD_MED_DEFAULT
        ),
        1,
    )
    threshold_large = max(
        _setting_int(
            session, "outage_confirm_threshold_large", CONFIRM_THRESHOLD_LARGE_DEFAULT
        ),
        threshold_med,
    )
    resolve = max(
        _setting_int(session, "outage_resolve_seconds", RESOLVE_SECONDS_DEFAULT), 0
    )
    return _Windows(
        confirm={
            "small": small,
            "med": med,
            "large": large,
            "threshold_med": threshold_med,
            "threshold_large": threshold_large,
        },
        resolve=resolve,
    )


def confirm_window_seconds(
    affected_count: int,
    *,
    small: int,
    med: int,
    large: int,
    threshold_med: int,
    threshold_large: int,
) -> int:
    """W_confirm in seconds for a given blast radius (design §7.6 decision 2).

    ``affected_count >= threshold_large`` confirms this cycle (``large``, 0s by
    default); ``threshold_med..threshold_large-1`` waits ``med``; below
    ``threshold_med`` waits ``small``.
    """
    if affected_count >= threshold_large:
        return large
    if affected_count >= threshold_med:
        return med
    return small


def _elapsed_seconds(now: datetime, ts: datetime | None) -> float:
    """Seconds since ``ts`` (tz-coerced). A missing stamp reads as 0 elapsed so a
    transition never fires off a null timestamp."""
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


@dataclass
class _Candidate:
    root_node: NetworkDevice
    basestation_id: object
    affected_count: int
    confidence: float | None
    classification: str
    # Nodes forming this candidate's connected-dark component (current deepest +
    # every dark node that localized to it + its downstream span) — the set the
    # identity rule matches a drifted incident root against.
    component_node_ids: set


def _candidate_outages(session: Session, now: datetime) -> dict:
    """Current classifier candidate outages, keyed by deepest-dark node id.

    One online-overlay pass over every active zabbix-linked node -> the nodes
    currently ``node_outage`` -> localize each to its deepest dark-under-live
    boundary. Whole-graph maps computed ONCE and reused (no per-candidate BFS).
    """
    nodes = (
        session.query(NetworkDevice)
        .filter(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.zabbix_hostid.isnot(None),
        )
        .all()
    )
    if not nodes:
        return {}
    node_ids = [n.id for n in nodes]
    by_node = subscriptions_for_nodes(session, node_ids)
    all_sub_ids = {s.id for subs in by_node.values() for s in subs}
    online_ids = online_subscription_ids(session, all_sub_ids, now=now)
    online_by_node = {
        nid: sum(1 for s in by_node.get(nid, []) if s.id in online_ids)
        for nid in node_ids
    }
    provisioned = {nid: len(by_node.get(nid, [])) for nid in node_ids}

    dark_nodes = [
        n
        for n in nodes
        if classify_node(n, online_by_node[n.id], provisioned[n.id] > 0) == NODE_OUTAGE
    ]
    if not dark_nodes:
        return {}

    dark_ids = {n.id for n in dark_nodes}
    adjacency = lldp_adjacency(session)
    dist = _dist_to_core(session, adjacency=adjacency)

    candidates: dict = {}
    for dn in dark_nodes:
        # Localize among the ``node_outage`` nodes in this dark node's downstream
        # span only — not the full scope. localize_outage reads "dark" as
        # proof-of-life collapse alone, so a mgmt-UP node merely serving nobody
        # (service_fault) would otherwise be mistaken for the outage boundary.
        # Restricting to node_outage nodes keeps the boundary a genuine outage.
        scope = downstream_nodes(session, dn, dist=dist, adjacency=adjacency)
        dark_in_scope = [nid for nid in scope if nid in dark_ids]
        loc = localize_outage(session, dark_in_scope, now=now)
        if loc is None:
            continue
        fid = loc["failure_node"]
        if fid in candidates:
            candidates[fid].component_node_ids.update(scope)
            candidates[fid].component_node_ids.add(dn.id)
            continue
        fnode = session.get(NetworkDevice, fid)
        if fnode is None:
            continue
        impact = affected_customers(session, node=fnode, dist=dist, adjacency=adjacency)
        # The identity-match component: everything below the current boundary,
        # plus this dark node's full span (which may sit ABOVE the boundary when
        # drift moved the root deeper) — so a drifted incident root, in either
        # direction, is still recognised as the same outage.
        component = set(
            downstream_nodes(session, fnode, dist=dist, adjacency=adjacency)
        )
        component.update(scope)
        component.add(dn.id)
        component.add(fid)
        candidates[fid] = _Candidate(
            root_node=fnode,
            basestation_id=fnode.pop_site_id,
            affected_count=impact["count"],
            confidence=_CONFIDENCE_SCORE.get(loc["confidence"]),
            classification=loc["class"],
            component_node_ids=component,
        )

    # Several distinct dark components under ONE basestation are ONE outage
    # (§7.6 decision 3: identity is the site). Merge them BEFORE find-or-open, or
    # the second component — its incident excluded via ``used`` — would open a
    # duplicate for the same basestation. Deepest boundary wins as root; union
    # the components; sum affected across the (disjoint) components.
    return _merge_by_basestation(candidates, dist)


def _depth_key(dist: dict, node_id) -> tuple:
    d = dist.get(node_id)
    return (d is not None, d if d is not None else -1)


def _merge_by_basestation(candidates: dict, dist: dict) -> dict:
    by_bts: dict = {}
    for fid, cand in candidates.items():
        if cand.basestation_id is not None:
            by_bts.setdefault(cand.basestation_id, []).append(fid)
    for fids in by_bts.values():
        if len(fids) < 2:
            continue
        # Deepest-dark boundary as the survivor/root; fold the rest into it.
        fids_sorted = sorted(fids, key=lambda f: _depth_key(dist, f), reverse=True)
        keep = candidates[fids_sorted[0]]
        for other in fids_sorted[1:]:
            merged = candidates.pop(other)
            keep.component_node_ids.update(merged.component_node_ids)
            keep.affected_count += merged.affected_count
    return candidates


def reconcile_detected_outages(
    session: Session, *, now: datetime | None = None
) -> dict[str, int]:
    """One reconcile pass over the classifier-driven incident lifecycle.

    Idempotent across (serialized) runs: identity dedupes candidates onto their
    existing incident, and every transition is guarded by the incident's current
    state, so a re-run with the same signals is a no-op. Per-item savepoints keep
    one failing candidate from rolling back the whole pass.
    """
    now = now or datetime.now(UTC)
    windows = _resolve_windows(session)
    counters: dict[str, int] = {
        "candidates": 0,
        "open_incidents": 0,
        "suspected_opened": 0,
        "confirmed": 0,
        "discarded": 0,
        "clearing": 0,
        "resolved": 0,
        "reopened": 0,
        "rerooted": 0,
        "errors": 0,
    }

    candidates = _candidate_outages(session, now)
    counters["candidates"] = len(candidates)

    # Snapshot the open classifier incidents BEFORE mutating — anything a
    # candidate does not claim this pass is a recovery to debounce down.
    open_incidents = (
        session.query(OutageIncident)
        .filter(
            OutageIncident.detection_source == CLASSIFIER_SOURCE,
            OutageIncident.status.in_(CLASSIFIER_OPEN_STATUSES),
        )
        .all()
    )
    counters["open_incidents"] = len(open_incidents)

    used: set = set()

    # --- 1. dark candidates: find-or-open, re-point on drift, debounce up ------
    for root_id, cand in candidates.items():
        try:
            with session.begin_nested():
                incident = find_open_classifier_incident(
                    session,
                    basestation_id=cand.basestation_id,
                    root_node_id=root_id,
                    component_node_ids=cand.component_node_ids,
                    exclude_ids=used,
                )
                if incident is None:
                    incident = open_classifier_incident(
                        session,
                        root_node=cand.root_node,
                        basestation_id=cand.basestation_id,
                        affected_count=cand.affected_count,
                        confidence=cand.confidence,
                        classification=cand.classification,
                        now=now,
                    )
                    counters["suspected_opened"] += 1
                else:
                    # Re-point the root to the current deepest-dark node on
                    # localization drift (node-scoped incidents only; a
                    # basestation incident's identity is the site, not the node).
                    if incident.basestation_id is None and repoint_root(
                        session, incident, cand.root_node
                    ):
                        counters["rerooted"] += 1
                    update_classifier_snapshot(
                        incident,
                        affected_count=cand.affected_count,
                        confidence=cand.confidence,
                        classification=cand.classification,
                    )
                    # Re-darkened inside the resolve window -> reopen (hysteresis).
                    if incident.status == "clearing":
                        reopen_incident(session, incident)
                        counters["reopened"] += 1
                used.add(incident.id)

                # Debounce up: suspected -> confirmed once W_confirm has elapsed.
                if incident.status == "suspected":
                    window = confirm_window_seconds(
                        incident.affected_count, **windows.confirm
                    )
                    if _elapsed_seconds(now, incident.suspected_at) >= window:
                        confirm_incident(session, incident, now=now)
                        counters["confirmed"] += 1
        except Exception:  # noqa: BLE001 - one bad candidate must not poison the run
            counters["errors"] += 1
            logger.exception(
                "outage_reconcile_candidate_failed", extra={"root_node": str(root_id)}
            )

    # --- 2. recovered incidents (no current candidate): debounce down ---------
    for incident in open_incidents:
        if incident.id in used:
            continue
        try:
            with session.begin_nested():
                if incident.status == "suspected":
                    # A suspected incident with no candidate this pass recovered.
                    # WHEN it recovered decides its fate: only a true pre-confirm
                    # blip (recovered before W_confirm) is a false positive to
                    # discard. If the suspicion already outlived W_confirm — the
                    # reconcile cadence (~180s) can be longer than a small/med
                    # confirm window, so a real sustained outage may never have
                    # got a pass to flip to confirmed — confirm THEN begin
                    # clearing, preserving confirmed_at (and hence MTTR) and
                    # letting the resolve debounce run.
                    window = confirm_window_seconds(
                        incident.affected_count, **windows.confirm
                    )
                    if _elapsed_seconds(now, incident.suspected_at) >= window:
                        confirm_incident(session, incident, now=now)
                        counters["confirmed"] += 1
                        start_clearing(session, incident, now=now)
                        counters["clearing"] += 1
                    else:
                        discard_incident(session, incident)
                        counters["discarded"] += 1
                elif incident.status == "confirmed":
                    start_clearing(session, incident, now=now)
                    counters["clearing"] += 1
                elif incident.status == "clearing":
                    if _elapsed_seconds(now, incident.cleared_at) >= windows.resolve:
                        resolve_classifier_incident(session, incident, now=now)
                        counters["resolved"] += 1
        except Exception:  # noqa: BLE001
            counters["errors"] += 1
            logger.exception(
                "outage_reconcile_recovery_failed",
                extra={"incident": str(incident.id)},
            )

    logger.info("outage_reconcile: %s", counters)
    return counters
