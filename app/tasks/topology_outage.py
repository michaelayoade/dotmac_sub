"""Scheduled outage auto-detection scan (Phase 5b).

Evaluates recent down-transitions (infra + radios) against the reachability
classification and opens auto-detected OutageIncidents for tripped scopes.
Routed to the ``ingestion`` queue like the other topology sweeps.

Single-flight: the run is guarded by ``db_session_adapter.advisory_lock``
(the repo's safe helper — rolls back before unlocking and wraps the unlock in
try/except, so the lock can never leak on an aborted transaction), mirroring
run_uisp_topology_sync / app/tasks/events.py. Without it, an overlapping run
(beat interval == hard time limit) could double-declare: the open-incident
check is a plain read and declare is a plain insert. An overlapping run is
skipped with a marker. Idempotency across (serialized) runs comes from the
open-incident check; the radio transition baseline is persisted only AFTER a
successful commit, so a failed run re-detects the same transitions instead of
losing them.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Statement timeout for the lock session; the scan's own statements are all
# small single-row/index lookups plus a handful of bounded sweeps.
_LOCK_TIMEOUT_MS = 30_000


@celery_app.task(
    name="app.tasks.topology_outage.run_outage_scan",
    soft_time_limit=240,
    time_limit=300,
)
def run_outage_scan() -> dict[str, Any]:
    """Run one auto-detection pass; commit created incidents on success."""
    from app.services.topology.outage_autodetect import (
        ADVISORY_LOCK_KEY,
        baseline_ttl_seconds,
        evaluate_with_cached_baseline,
        store_radio_baseline,
    )

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("topology_outage_scan_skipped: previous run still in progress")
            return {"skipped": "already_running"}
        try:
            result, baseline = evaluate_with_cached_baseline(db)
            ttl = baseline_ttl_seconds(db)
            db.commit()
            store_radio_baseline(baseline, ttl_seconds=ttl)
            return result
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("topology_outage_scan_timed_out")
            return {"error": "topology_outage_scan_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("topology_outage_scan_failed")
            return {"error": str(exc)}


@celery_app.task(
    name="app.tasks.topology_outage.reconcile_detected_outages",
    soft_time_limit=150,
    time_limit=180,
)
def reconcile_detected_outages() -> dict[str, Any]:
    """Run one §7.6 debounce pass over the classifier-driven incident lifecycle.

    Discover-reconcile: classify -> localize -> find-or-open -> debounce the
    suspected/confirmed/clearing/resolved/discarded transitions, persisting a
    trustworthy incident and emitting lifecycle events. Firing (notify/ticket)
    stays GATED. Single-flight via the same advisory-lock helper as the scan; an
    overlapping run is skipped (transitions are guarded reads + writes)."""
    from app.services.topology.outage_reconcile import (
        ADVISORY_LOCK_KEY,
    )
    from app.services.topology.outage_reconcile import (
        reconcile_detected_outages as _reconcile,
    )

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("outage_reconcile_skipped: previous run still in progress")
            return {"skipped": "already_running"}
        try:
            result = _reconcile(db)
            db.commit()
            return result
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("outage_reconcile_timed_out")
            return {"error": "outage_reconcile_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("outage_reconcile_failed")
            return {"error": str(exc)}
