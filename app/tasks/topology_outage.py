"""Detected-outage incident reconcile task (design §7.6).

The single auto-detection loop: the classifier lifecycle (suspected ->
confirmed -> clearing -> resolved/discarded) debounced by
``outage_reconcile``, fed by both detector arms (dark nodes + wireless
clusters). The old open-only auto-detect scan is retired — its unique
wireless coverage moved into the reconcile's candidate generation, so every
auto incident now self-resolves.

Single-flight via ``db_session_adapter.advisory_lock`` (the repo's safe
helper), mirroring the other topology sweeps.
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
