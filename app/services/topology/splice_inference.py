"""Splice inference — outage classifier P3 (design §6, docs/designs/OUTAGE_CLASSIFIER.md).

The OLT sees every ONT's status + optical Rx, but the passive splitter's internal
sub-split branch/splice is UNPOLLABLE and the manual ``SplitterPortAssignment``
plant records rot (design §4). Recover that hidden sub-PON topology from OLT
per-ONT telemetry over time (``ont_signal_observations``):

  1. ``infer_branches``    — co-failure clustering: ONTs that repeatedly go dark
     *together* (while the rest of the PON stays up) share a branch.
  2. ``detect_rx_droop``   — correlated Rx shift: a bend/failing splice upstream
     of a branch attenuates every ONT beyond it by the SAME dB. A cluster of
     matching Rx droop is a dying branch, caught BEFORE it cuts (predictive).
  3. ``reconcile_with_records`` — diff the inferred grouping against the plant
     records (diff-not-mirror). The inference BECOMES the plant map that was
     never maintained; it only SURFACES disagreement, never writes the records.

Everything here is deterministic (documented thresholds below, no randomness).
It reads the append-only time series that
``app.tasks.ont_signal_observations.record_ont_observations`` collects.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OntSignalObservation, OntUnit, OnuOnlineStatus

# --- thresholds (design §6; tune from field data) -------------------------

# How far back the inference looks by default.
DEFAULT_WINDOW = timedelta(days=30)

# Observations closer together than this collapse into one "sweep episode" — the
# collector emits a burst of rows per sweep at ~the same instant; this tolerates
# the spread without treating one outage as many.
EPISODE_BUCKET = timedelta(minutes=15)

# A pair of ONTs must co-fail (both dark in the same episode, while the PON is
# only PARTIALLY dark) at least this many times before we call them one branch.
MIN_COFAILURES = 2

# Rx droop (dBm, magnitude). An ONT must lose at least this much to count as
# drooping, and two drooping ONTs are "correlated" when their losses agree to
# within the tolerance — a shared upstream splice attenuates every ONT beyond it
# by the same amount.
RX_DROOP_MIN_DB = 2.0
RX_CORRELATION_TOLERANCE_DB = 1.0

# Branches are shared elements: a single ONT is a last-mile drop, not a splice.
MIN_BRANCH_SIZE = 2


def _confidence(support: int) -> str:
    """Map co-failure support count to a coarse confidence band."""
    if support >= 4:
        return "high"
    if support >= MIN_COFAILURES:
        return "medium"
    return "low"


def _connected_components(
    nodes: set[uuid.UUID], edges: dict[tuple[uuid.UUID, uuid.UUID], int]
) -> list[set[uuid.UUID]]:
    """Union-find over the co-failure graph -> branch clusters (deterministic)."""
    parent: dict[uuid.UUID, uuid.UUID] = {n: n for n in nodes}

    def find(x: uuid.UUID) -> uuid.UUID:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: uuid.UUID, b: uuid.UUID) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Point the larger uuid at the smaller for a stable representative.
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo

    for a, b in edges:
        union(a, b)

    groups: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for n in nodes:
        groups[find(n)].add(n)
    return [g for g in groups.values() if len(g) >= MIN_BRANCH_SIZE]


def _load_observations(
    session: Session,
    pon_port_id: uuid.UUID,
    *,
    window: timedelta,
    now: datetime | None,
) -> list[OntSignalObservation]:
    cutoff = (now or datetime.now(UTC)) - window
    return list(
        session.execute(
            select(OntSignalObservation)
            .where(
                OntSignalObservation.pon_port_id == pon_port_id,
                OntSignalObservation.observed_at >= cutoff,
            )
            .order_by(OntSignalObservation.observed_at)
        ).scalars()
    )


def _episode_key(observed_at: datetime) -> int:
    """Bucket an observation timestamp into a sweep episode index."""
    return int(observed_at.timestamp() // EPISODE_BUCKET.total_seconds())


def infer_branches(
    session: Session,
    pon_port_id: uuid.UUID,
    *,
    window: timedelta = DEFAULT_WINDOW,
    now: datetime | None = None,
) -> list[dict]:
    """Co-failure clusters on a PON — ONTs that repeatedly go dark together.

    Groups the PON's observations into sweep episodes; in each episode where the
    PON is only PARTIALLY dark (some ONTs offline, some online — a whole-PON
    outage is a feeder/OLT fault, not a branch, so it is skipped), every pair of
    co-offline ONTs earns a co-failure. Pairs reaching ``MIN_COFAILURES`` become
    edges; connected components of size >= ``MIN_BRANCH_SIZE`` are inferred
    branches. Returns, deepest-support first::

        [{"ont_unit_ids": [...], "support": int, "confidence": str}, ...]
    """
    obs = _load_observations(session, pon_port_id, window=window, now=now)

    # episode -> {ont_id: is_offline}
    episodes: dict[int, dict[uuid.UUID, bool]] = defaultdict(dict)
    all_onts: set[uuid.UUID] = set()
    for o in obs:
        all_onts.add(o.ont_unit_id)
        offline = o.olt_status == OnuOnlineStatus.offline
        # If an ONT appears twice in one episode, a single offline reading is
        # enough to count it dark for that episode.
        episodes[_episode_key(o.observed_at)][o.ont_unit_id] = (
            episodes[_episode_key(o.observed_at)].get(o.ont_unit_id, False) or offline
        )

    pair_cofail: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
    for states in episodes.values():
        offline_here = sorted(oid for oid, dark in states.items() if dark)
        online_here = [oid for oid, dark in states.items() if not dark]
        # Partial-PON only: a shared-branch signature needs survivors. No
        # survivors this episode -> whole-PON/feeder outage, not a branch.
        if len(offline_here) < 2 or not online_here:
            continue
        for i in range(len(offline_here)):
            for j in range(i + 1, len(offline_here)):
                pair_cofail[(offline_here[i], offline_here[j])] += 1

    edges = {pair: n for pair, n in pair_cofail.items() if n >= MIN_COFAILURES}
    clusters = _connected_components(all_onts, edges)

    results = []
    for cluster in clusters:
        # Support = weakest co-failure link holding the cluster together.
        internal = [n for (a, b), n in edges.items() if a in cluster and b in cluster]
        support = min(internal) if internal else 0
        results.append(
            {
                "ont_unit_ids": sorted(cluster, key=str),
                "support": support,
                "confidence": _confidence(support),
            }
        )
    results.sort(key=lambda r: cast(int, r["support"]), reverse=True)
    return results


def detect_rx_droop(
    session: Session,
    pon_port_id: uuid.UUID,
    *,
    window: timedelta = DEFAULT_WINDOW,
    now: datetime | None = None,
) -> list[dict]:
    """Correlated Rx droop on a PON — a dying branch, caught before it cuts.

    For each ONT with readings spanning the window, delta = latest - baseline
    (baseline = earliest reading). ONTs losing at least ``RX_DROOP_MIN_DB`` are
    grouped where their losses agree within ``RX_CORRELATION_TOLERANCE_DB`` — a
    shared upstream splice attenuates every ONT beyond it by the SAME dB.
    Returns, largest cluster first::

        [{"ont_unit_ids": [...], "shared_shift_db": float, "confidence": str}]
    """
    obs = _load_observations(session, pon_port_id, window=window, now=now)

    # ont -> (earliest reading, latest reading) with valid Rx.
    first: dict[uuid.UUID, float] = {}
    last: dict[uuid.UUID, float] = {}
    for o in obs:  # already ordered by observed_at
        if o.rx_signal_dbm is None:
            continue
        if o.ont_unit_id not in first:
            first[o.ont_unit_id] = o.rx_signal_dbm
        last[o.ont_unit_id] = o.rx_signal_dbm

    # Drooping ONTs only (magnitude >= threshold, i.e. lost signal).
    droops: dict[uuid.UUID, float] = {}
    for oid, base in first.items():
        delta = last[oid] - base  # negative == droop
        if delta <= -RX_DROOP_MIN_DB:
            droops[oid] = delta

    # Greedy single-link clustering over the 1-D delta axis: sort by delta and
    # start a new cluster whenever the gap to the previous exceeds tolerance.
    ordered = sorted(droops.items(), key=lambda kv: (kv[1], str(kv[0])))
    clusters: list[list[tuple[uuid.UUID, float]]] = []
    for oid, delta in ordered:
        if clusters and abs(delta - clusters[-1][-1][1]) <= RX_CORRELATION_TOLERANCE_DB:
            clusters[-1].append((oid, delta))
        else:
            clusters.append([(oid, delta)])

    results = []
    for cluster in clusters:
        if len(cluster) < MIN_BRANCH_SIZE:
            continue
        shift = sum(d for _, d in cluster) / len(cluster)
        results.append(
            {
                "ont_unit_ids": sorted((oid for oid, _ in cluster), key=str),
                "shared_shift_db": round(shift, 2),
                "confidence": "high" if len(cluster) >= 3 else "medium",
            }
        )
    results.sort(key=lambda r: len(cast(list, r["ont_unit_ids"])), reverse=True)
    return results


def _record_branches(
    session: Session, pon_port_id: uuid.UUID
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Plant-record grouping: ONTs on the PON keyed by their splitter port.

    ``OntUnit.splitter_port_id`` is the maintained plant map (fed from
    ``SplitterPortAssignment``): which splitter branch each ONT hangs off. Only
    ports carrying >= ``MIN_BRANCH_SIZE`` ONTs are shared branches.
    """
    rows = session.execute(
        select(OntUnit.id, OntUnit.splitter_port_id).where(
            OntUnit.pon_port_id == pon_port_id,
            OntUnit.splitter_port_id.is_not(None),
        )
    ).all()
    by_port: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for ont_id, port_id in rows:
        by_port[port_id].add(ont_id)
    return {
        port: onts for port, onts in by_port.items() if len(onts) >= MIN_BRANCH_SIZE
    }


def reconcile_with_records(
    session: Session,
    pon_port_id: uuid.UUID,
    *,
    window: timedelta = DEFAULT_WINDOW,
    now: datetime | None = None,
) -> dict:
    """Diff inferred branches against plant records (diff-not-mirror, design §6).

    Two branches are the same branch when they share >= ``MIN_BRANCH_SIZE`` ONTs.
    NEVER writes the plant records — it only surfaces disagreement::

        {
          "agrees":              [{"ont_unit_ids": [...]}],   # inference confirms record
          "missing_in_records":  [{"ont_unit_ids": [...]}],   # reality shows a branch records lack
          "missing_in_reality":  [{"splitter_port_id", "ont_unit_ids"}],  # record telemetry can't confirm
        }
    """
    inferred = infer_branches(session, pon_port_id, window=window, now=now)
    records = _record_branches(session, pon_port_id)

    def overlaps(a: set[uuid.UUID], b: set[uuid.UUID]) -> bool:
        return len(a & b) >= MIN_BRANCH_SIZE

    inferred_sets = [set(b["ont_unit_ids"]) for b in inferred]
    matched_records: set[uuid.UUID] = set()
    agrees: list[dict] = []
    missing_in_records: list[dict] = []

    for iset in inferred_sets:
        hit = next(
            (port for port, rset in records.items() if overlaps(iset, rset)), None
        )
        if hit is not None:
            matched_records.add(hit)
            agrees.append({"ont_unit_ids": sorted(iset, key=str)})
        else:
            # Telemetry says these share a branch; no plant record groups them.
            missing_in_records.append({"ont_unit_ids": sorted(iset, key=str)})

    missing_in_reality = [
        {"splitter_port_id": port, "ont_unit_ids": sorted(onts, key=str)}
        for port, onts in records.items()
        if port not in matched_records
    ]

    return {
        "agrees": agrees,
        "missing_in_records": missing_in_records,
        "missing_in_reality": missing_in_reality,
    }
