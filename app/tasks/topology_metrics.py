"""Scheduled topology coverage/pipeline-health metrics export.

Pushes per-medium E2E coverage gauges + feeder-task health gauges to
VictoriaMetrics (see app/services/topology/coverage_metrics.py). Routed to
the ``ingestion`` queue like the other topology tasks. Read-only against the
database; the only side effect is the VM push.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_metrics.export_topology_metrics",
    soft_time_limit=240,
    time_limit=300,
)
def export_topology_metrics() -> dict[str, Any]:
    """Collect topology coverage + pipeline gauges and push them to VM."""
    from app.services.topology.coverage_metrics import (
        export_topology_metrics as _export,
    )

    db = db_session_adapter.create_session()
    try:
        return _export(db)
    except SoftTimeLimitExceeded:
        logger.warning("topology_metrics_export_timed_out")
        return {"error": "topology_metrics_export_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report, never retry-loop
        logger.exception("topology_metrics_export_failed")
        return {"error": str(exc)}
    finally:
        db.close()
