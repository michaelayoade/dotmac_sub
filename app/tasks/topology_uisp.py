"""Scheduled UISP topology sync -> cpe_devices/olt_devices/ont_units edges.

Pulls the UISP inventory (read-only) and reconciles the wireless/UFiber
customer-device relationship layer into sub's own tables. Routed to the
``ingestion`` queue like the other topology tasks.

The pass commits at phase boundaries rather than once at the end: it fans out
one UISP request per AP and one per UF-OLT, and a transaction may not be held
across that. A failed pass can therefore leave earlier phases committed, which
is safe because the sync is idempotent — see ``uisp_sync.sync``.

Single-flight: the run is guarded by ``db_session_adapter.advisory_lock``
(the repo's safe helper — rolls back before unlocking and wraps the unlock in
try/except, so the lock can never leak on an aborted transaction). An
overlapping scheduled/on-demand run is skipped, mirroring app/tasks/events.py.
"""

from __future__ import annotations

import logging

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import record_task_run
from app.services.operational_logging import (
    OperationalEventName,
    OperationalLogEvent,
    OperationalOutcome,
    log_operational_event,
)
from app.services.topology.coverage_metrics import store_task_stats
from app.services.uisp import UispClient, UispClientError, uisp_configured

logger = logging.getLogger(__name__)

# Statement timeout for acquiring the advisory lock. Applied with SET LOCAL,
# so it also bounds the sync's statements up to the first phase commit and no
# further; the task's own soft/hard time limits bound the rest of the run. It
# is deliberately not made session-level: this connection returns to the pool,
# and a lingering statement_timeout would leak to whoever checks it out next.
_LOCK_TIMEOUT_MS = 30_000


def _counter_summary(value: object) -> dict[str, int]:
    """Normalize the sync's numeric counter transport once, without coercion."""
    if not isinstance(value, dict):
        raise TypeError("UISP sync returned an invalid counter summary")
    counters: dict[str, int] = {}
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise TypeError("UISP sync returned an invalid counter summary")
        counters[key] = count
    return counters


def _report_outcome(outcome: OperationalOutcome, *, counters: dict[str, int]) -> None:
    log_operational_event(
        logger,
        OperationalLogEvent(
            name=OperationalEventName.UISP_TOPOLOGY_SYNC_COMPLETED,
            outcome=outcome,
            component="uisp",
            counters=counters,
        ),
    )
    record_task_run(
        "app.tasks.topology_uisp.run_uisp_topology_sync",
        status=outcome.recording_status,
        counters=counters,
    )


@celery_app.task(
    name="app.tasks.topology_uisp.run_uisp_topology_sync",
    soft_time_limit=540,
    time_limit=600,
)
def run_uisp_topology_sync() -> dict[str, int | str]:
    """Sync UISP customer-device topology into sub's tables."""
    if not uisp_configured():
        _report_outcome(OperationalOutcome.SKIPPED, counters={})
        return {
            "skipped": "uisp_token_missing",
            "operational_outcome": OperationalOutcome.SKIPPED.value,
        }

    from app.services.topology.uisp_sync import ADVISORY_LOCK_KEY, sync

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("uisp_topology_sync_skipped: previous run still in progress")
            _report_outcome(OperationalOutcome.SKIPPED, counters={})
            return {
                "skipped": "already_running",
                "operational_outcome": OperationalOutcome.SKIPPED.value,
            }
        result: dict[str, int | str]
        counters: dict[str, int] = {}
        try:
            client = UispClient.from_env()
            counters = _counter_summary(sync(db, client))
            db.commit()
            # These are explicit failure counters, not unmatched radios,
            # reviewed topology disagreements or protected prune decisions.
            failures = sum(
                counters.get(key, 0)
                for key in ("failed", "port_fetch_failures", "link_fetch_failures")
            )
            outcome = (
                OperationalOutcome.PARTIAL if failures else OperationalOutcome.COMPLETED
            )
            result = dict(counters)
        except UispClientError:
            db.rollback()
            outcome = OperationalOutcome.FAILED
            counters = {"failed_runs": 1}
            result = {"error": "uisp_unavailable", "message": "UISP API request failed"}
        except SoftTimeLimitExceeded:
            db.rollback()
            outcome = OperationalOutcome.FAILED
            counters = {"timed_out": 1}
            result = {"error": "uisp_topology_sync_timed_out"}
        except Exception:
            db.rollback()
            # Earlier phases may already have committed. Preserve factual
            # failure evidence and let Celery fail, without adding autoretry.
            store_task_stats("uisp_sync", {"error": "uisp_topology_sync_failed"})
            _report_outcome(OperationalOutcome.FAILED, counters={"failed_runs": 1})
            raise
        # Preserve the existing cache's numeric/error shape. The explicit
        # framework outcome belongs only to the returned adapter envelope.
        store_task_stats("uisp_sync", result)
        _report_outcome(outcome, counters=counters)
        return {**result, "operational_outcome": outcome.value}
