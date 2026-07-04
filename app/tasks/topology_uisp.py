"""Scheduled UISP topology sync -> cpe_devices/olt_devices/ont_units edges.

Pulls the UISP inventory (read-only) and reconciles the wireless/UFiber
customer-device relationship layer into sub's own tables. Routed to the
``ingestion`` queue like the other topology tasks; commits on success.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.uisp import UispClient, UispClientError, uisp_configured

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_uisp.run_uisp_topology_sync",
    soft_time_limit=540,
    time_limit=600,
)
def run_uisp_topology_sync() -> dict[str, Any]:
    """Sync UISP customer-device topology into sub's tables."""
    if not uisp_configured():
        return {"skipped": "uisp_token_missing"}

    from app.services.topology.uisp_sync import sync

    db = db_session_adapter.create_session()
    try:
        client = UispClient.from_env()
        result = sync(db, client)
        db.commit()
        return result
    except UispClientError as exc:
        db.rollback()
        logger.warning("uisp_topology_sync_failed: %s", exc)
        return {"error": "uisp_unavailable", "message": str(exc)}
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("uisp_topology_sync_timed_out")
        return {"error": "uisp_topology_sync_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("uisp_topology_sync_failed")
        return {"error": str(exc)}
    finally:
        db.close()
