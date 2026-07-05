"""Scheduled outage auto-detection scan (Phase 5b).

Evaluates recent down-transitions (infra + radios) against the reachability
classification and opens auto-detected OutageIncidents for tripped scopes.
Routed to the ``ingestion`` queue like the other topology sweeps. Idempotent
across runs: an ongoing outage is covered by its open incident, so re-runs
skip it. The radio transition baseline is persisted only AFTER a successful
commit, so a failed run re-detects the same transitions instead of losing
them.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_outage.run_outage_scan",
    soft_time_limit=240,
    time_limit=300,
)
def run_outage_scan() -> dict[str, Any]:
    """Run one auto-detection pass; commit created incidents on success."""
    from app.services.topology.outage_autodetect import (
        baseline_ttl_seconds,
        evaluate_with_cached_baseline,
        store_radio_baseline,
    )

    db = db_session_adapter.create_session()
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
    finally:
        db.close()
