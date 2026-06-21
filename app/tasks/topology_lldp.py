"""Scheduled LLDP neighbor poll -> directed NetworkTopologyLink (Phase 2, P2.5).

Reads each MikroTik NAS's /ip/neighbor and reconciles the device-level directed
graph. Routed to the ``ingestion`` queue. Read-only against routers; commits the
edge upsert on success.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_lldp.run_lldp_topology_poll",
    soft_time_limit=300,
    time_limit=360,
)
def run_lldp_topology_poll() -> dict[str, Any]:
    """Poll the fleet's LLDP neighbors and reconcile directed links."""
    from app.services.topology.lldp_poller import poll_all

    db = db_session_adapter.create_session()
    try:
        result = poll_all(db)
        db.commit()
        return result
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("lldp_topology_poll_timed_out")
        return {"error": "lldp_topology_poll_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("lldp_topology_poll_failed")
        return {"error": str(exc)}
    finally:
        db.close()
