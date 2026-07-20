"""Scheduled topology status warmer.

Refreshes the cached live_status overlay for topology nodes from native poll
results. Routed to the ``ingestion`` queue, which has a live consumer. (The
Zabbix topology reconcile that used to live here was retired with the native
monitoring cutover.)
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_sync.warm_topology_status",
    soft_time_limit=110,
    time_limit=150,
)
def warm_topology_status() -> dict[str, Any]:
    """Refresh cached live_status for topology nodes from native poll results."""
    from app.services.topology.live_status import (
        touch_warm_heartbeat,
    )
    from app.services.topology.live_status import (
        warm_topology_status as _warm,
    )

    try:
        with db_session_adapter.session() as db:
            result = _warm(db)
        # Stamp the heartbeat only after a successful refresh so a stalled/failing
        # warmer ages out and stops good states being trusted (see selfcare).
        touch_warm_heartbeat()
        return result
    except SoftTimeLimitExceeded:
        logger.warning("topology_status_warm_timed_out")
        return {"error": "topology_status_warm_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        logger.exception("topology_status_warm_failed")
        return {"error": str(exc)}
