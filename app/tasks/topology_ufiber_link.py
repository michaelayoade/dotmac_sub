"""Scheduled UFiber ONU -> subscriber link pass.

Standalone, net-new reconciler that fills the missing ``ont_assignments`` link
for router-mode UFiber (UF-Wifi) ONUs by matching the ONU's own MAC (from UISP,
already in ``ont_units.mac_address``) to the PPPoE calling-station-id RADIUS
authenticated (``subscriptions.mac_address``). See
``app.services.topology.ufiber_onu_link`` for the auth-safety rationale — this
pass NEVER writes ``subscriptions.mac_address``.

Routed to the ``ingestion`` queue like the other topology tasks; commits on
success. Single-flight via ``db_session_adapter.advisory_lock`` (the repo's safe
helper — rolls back before unlocking and wraps the unlock in try/except so the
lock can never leak on an aborted transaction). An overlapping scheduled or
on-demand run is skipped, mirroring app/tasks/topology_uisp.py.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.topology.coverage_metrics import store_task_stats

logger = logging.getLogger(__name__)

# Statement timeout for the lock session; also bounds the pass's own statements
# (a single MAC-index build plus per-ONU savepointed inserts).
_LOCK_TIMEOUT_MS = 30_000


@celery_app.task(
    name="app.tasks.topology_ufiber_link.run_ufiber_onu_link",
    soft_time_limit=540,
    time_limit=600,
)
def run_ufiber_onu_link() -> dict[str, Any]:
    """Link router-mode UFiber ONUs to their active subscriber by ONU MAC."""
    from app.services.topology.ufiber_onu_link import (
        ADVISORY_LOCK_KEY,
        link_ufiber_onus_to_subscribers,
    )

    with db_session_adapter.advisory_lock(
        ADVISORY_LOCK_KEY, timeout_ms=_LOCK_TIMEOUT_MS
    ) as (db, acquired):
        if not acquired:
            logger.info("ufiber_onu_link_skipped: previous run still in progress")
            return {"skipped": "already_running"}
        try:
            result = link_ufiber_onus_to_subscribers(db)
            db.commit()
        except SoftTimeLimitExceeded:
            db.rollback()
            logger.warning("ufiber_onu_link_timed_out")
            result = {"error": "ufiber_onu_link_timed_out"}
        except Exception as exc:  # noqa: BLE001 - report and roll back
            db.rollback()
            logger.exception("ufiber_onu_link_failed")
            result = {"error": str(exc)}
        # Stash the run outcome for the topology metrics exporter; lock-skips
        # above never reach here, so they can't clobber the last real result.
        store_task_stats("ufiber_onu_link", result)
        return result
