"""Topology E2E coverage + pipeline-health metrics -> VictoriaMetrics.

Turns the topology-gaps report (a page someone has to remember to read) into
gauges VictoriaMetrics scrapes alerts from, so a coverage regression or a
silently failing feeder task pages instead of rotting. Two halves:

- Coverage: per-medium E2E match-rate derived from the SAME batched
  classification the gaps page renders (``gaps.classify_active_subscriptions``)
  so the number on the dashboard can never disagree with the report.
- Pipeline health: last-run counters + staleness for the feeder tasks
  (uisp_sync, lldp_poll). The wrappers stash their returned stats dict in
  app_cache via ``store_task_stats``; the exporter re-emits them as labeled
  gauges. Motivating incident: a uisp_sync run returned ``failed=629`` while
  the Celery task itself "succeeded" — nothing looked at the dict, nobody
  noticed.

Push path: the existing sync VictoriaMetrics writer used by
``app.tasks.bandwidth.aggregate_to_metrics`` (``VictoriaMetricsWriter`` in
app/services/bandwidth_metrics_adapter.py, VICTORIAMETRICS_URL config). No new
transport is introduced.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import CPEDevice
from app.models.network_monitoring import NetworkTopologyLink
from app.services import app_cache
from app.services.bandwidth_metrics_adapter import (
    VictoriaMetricsWriter,
    WriteResult,
)
from app.services.topology.customer_path import (
    GAP_NO_BASESTATION,
    GAP_NO_NODE,
    GAP_NO_ONT,
)
from app.services.topology.gaps import (
    MEDIUM_FIBER,
    MEDIUM_NAS,
    MEDIUM_UNKNOWN,
    MEDIUM_WIRELESS,
    classify_active_subscriptions,
)
from app.services.topology.lldp_poller import SOURCE as LLDP_SOURCE

logger = logging.getLogger(__name__)

# Emitted for staleness/freshness when a feeder has never run (no cached
# stats, no synced rows). Deliberately absurd (~31.7 years) so a single
# `> threshold` alert rule fires on both "stale" and "never ran".
NEVER_RUN_SENTINEL_SECONDS = 1_000_000_000.0

# Feeder tasks whose wrappers stash last-run stats (label value == cache key
# suffix == `task` label on the emitted series).
TRACKED_TASKS = ("uisp_sync", "lldp_poll")

# Stash TTL. Long enough that a weekend outage still shows real staleness
# (not the sentinel), short enough that decommissioned feeders age out.
_TASK_STATS_TTL_SECONDS = 7 * 24 * 3600

# Full label cross-product is always emitted (zeros included) so a gap kind
# that empties out drops the gauge to 0 instead of leaving a stale non-zero
# last-written point in VictoriaMetrics.
MEDIUMS = (MEDIUM_FIBER, MEDIUM_WIRELESS, MEDIUM_NAS, MEDIUM_UNKNOWN)
GAP_KINDS = (GAP_NO_ONT, GAP_NO_NODE, GAP_NO_BASESTATION)

VM_WRITE_MAX_ATTEMPTS = 3

_writer: VictoriaMetricsWriter | None = None


def _get_writer() -> VictoriaMetricsWriter:
    """Module singleton over the shared sync VM writer (same URL/config as
    the bandwidth push; see module docstring)."""
    global _writer
    if _writer is None:
        _writer = VictoriaMetricsWriter()
    return _writer


# --- Task-stats stash (written by the topology task wrappers) ---------------


def _task_stats_key(task: str) -> str:
    return app_cache.cache_key("topology", "task_stats", task)


def store_task_stats(task: str, stats: dict | None) -> bool:
    """Stash a topology feeder task's last returned stats dict + timestamp.

    Called by the task wrappers right after a run attempt (success or error
    outcome — NOT lock-skips, which would clobber the last real result).
    Never raises: metrics stashing must not fail the task that did the work.
    """
    payload = {
        "stats": stats or {},
        "stored_at": datetime.now(UTC).timestamp(),
    }
    try:
        return app_cache.set_json(
            _task_stats_key(task), payload, _TASK_STATS_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - best-effort observability
        logger.debug("topology_task_stats_store_failed task=%s: %s", task, exc)
        return False


def read_task_stats(task: str) -> dict | None:
    """Read a feeder task's stashed {stats, stored_at}; None when never run
    (or aged out past the stash TTL)."""
    payload = app_cache.get_json(_task_stats_key(task))
    if not isinstance(payload, dict):
        return None
    return payload


# --- Sample collection -------------------------------------------------------

# A sample is (metric_name, labels, value); formatting to Prometheus text
# lines is kept separate so tests can assert on structure, not strings.
Sample = tuple[str, dict[str, str], float]


def _coverage_samples(db: Session) -> list[Sample]:
    """Per-medium active/gapped counts + E2E coverage ratio, derived from the
    exact classification the gaps page renders."""
    active: dict[str, int] = dict.fromkeys(MEDIUMS, 0)
    complete: dict[str, int] = dict.fromkeys(MEDIUMS, 0)
    gapped: dict[tuple[str, str], int] = {
        (medium, gap): 0 for medium in MEDIUMS for gap in GAP_KINDS
    }
    for row in classify_active_subscriptions(db):
        medium = row["medium"]
        active[medium] = active.get(medium, 0) + 1
        gap = row["gap"]
        if gap is None:
            complete[medium] = complete.get(medium, 0) + 1
        else:
            gapped[(medium, gap)] = gapped.get((medium, gap), 0) + 1

    samples: list[Sample] = []
    for medium in active:
        samples.append(
            ("topology_subscribers_active", {"medium": medium}, float(active[medium]))
        )
        if active[medium]:
            samples.append(
                (
                    "topology_e2e_coverage_ratio",
                    {"medium": medium},
                    complete.get(medium, 0) / active[medium],
                )
            )
    for (medium, gap), count in gapped.items():
        samples.append(
            (
                "topology_subscribers_gapped",
                {"medium": medium, "gap": gap},
                float(count),
            )
        )
    return samples


def _counter_value(value: Any) -> float:
    """Map a stats-dict value to a gauge value: numbers pass through,
    anything else (error strings, skip markers) becomes presence=1."""
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _task_health_samples(now: float) -> list[Sample]:
    """Last-run counters + staleness per tracked feeder task."""
    samples: list[Sample] = []
    for task in TRACKED_TASKS:
        payload = read_task_stats(task)
        stored_at = payload.get("stored_at") if payload else None
        if isinstance(stored_at, (int, float)):
            staleness = max(now - float(stored_at), 0.0)
        else:
            staleness = NEVER_RUN_SENTINEL_SECONDS
        samples.append(("topology_task_staleness_seconds", {"task": task}, staleness))
        stats = payload.get("stats") if payload else None
        if isinstance(stats, dict):
            for counter, value in sorted(stats.items()):
                samples.append(
                    (
                        "topology_task_last_result",
                        {"task": task, "counter": str(counter)},
                        _counter_value(value),
                    )
                )
    return samples


def _as_epoch(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def _source_freshness_samples(db: Session, now: float) -> list[Sample]:
    """Seconds since the last uisp_sync data write / lldp edge refresh —
    one MAX() query each; sentinel when no rows exist yet."""
    last_uisp = _as_epoch(
        db.execute(select(func.max(CPEDevice.uisp_synced_at))).scalar()
    )
    last_lldp = _as_epoch(
        db.execute(
            select(func.max(NetworkTopologyLink.last_seen_at)).where(
                NetworkTopologyLink.source == LLDP_SOURCE
            )
        ).scalar()
    )
    samples: list[Sample] = []
    for source, last in (("uisp", last_uisp), ("lldp", last_lldp)):
        freshness = (
            max(now - last, 0.0) if last is not None else NEVER_RUN_SENTINEL_SECONDS
        )
        samples.append(
            ("topology_source_freshness_seconds", {"source": source}, freshness)
        )
    return samples


def collect_topology_metrics(db: Session, *, now: float | None = None) -> list[Sample]:
    """All topology gauge samples for one export run."""
    now_ts = now if now is not None else datetime.now(UTC).timestamp()
    samples = _coverage_samples(db)
    samples.extend(_task_health_samples(now_ts))
    samples.extend(_source_freshness_samples(db, now_ts))
    return samples


# --- Prometheus formatting + push --------------------------------------------


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_prometheus_line(sample: Sample, timestamp_ms: int) -> str:
    name, labels, value = sample
    rendered = ",".join(
        f'{key}="{_escape_label_value(str(val))}"'
        for key, val in sorted(labels.items())
    )
    return f"{name}{{{rendered}}} {value} {timestamp_ms}"


def export_topology_metrics(db: Session, *, now: float | None = None) -> dict:
    """Collect and push one run's topology gauges to VictoriaMetrics.

    Mirrors app.tasks.bandwidth.aggregate_to_metrics: single batched push
    with a short retry for transient VM blips.
    """
    now_ts = now if now is not None else datetime.now(UTC).timestamp()
    samples = collect_topology_metrics(db, now=now_ts)
    timestamp_ms = int(now_ts * 1000)
    lines = [format_prometheus_line(sample, timestamp_ms) for sample in samples]

    writer = _get_writer()
    result: WriteResult = writer.write_prometheus_lines(
        lines, adapter="topology.metrics", operation="export_topology_metrics"
    )
    attempts = 1
    while not result.success and attempts < VM_WRITE_MAX_ATTEMPTS:
        time.sleep(0.5 * attempts)
        attempts += 1
        result = writer.write_prometheus_lines(
            lines, adapter="topology.metrics", operation="export_topology_metrics"
        )

    if result.success:
        logger.info(
            "topology_metrics_exported series=%d (attempt %d)", result.written, attempts
        )
    else:
        logger.error(
            "topology_metrics_export_failed after %d attempts: %s",
            attempts,
            result.error,
        )
    return {"series": len(lines), "pushed": result.written, "success": result.success}
