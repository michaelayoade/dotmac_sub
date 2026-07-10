"""Manual-trigger mgmt-IP backfill from UISP (inventory cleanup workstream).

Not beat-scheduled: run once (optionally as a dry run first), verify the
poll sweep picks the devices up, done. Kept as a Celery task so it runs on a
worker with UISP credentials and survives operator disconnects.

    from app.tasks.uisp_ip_backfill import run_uisp_mgmt_ip_backfill
    run_uisp_mgmt_ip_backfill.delay(dry_run=True)
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.uisp_ip_backfill.run_uisp_mgmt_ip_backfill",
    soft_time_limit=240,
    time_limit=300,
)
def run_uisp_mgmt_ip_backfill(dry_run: bool = False) -> dict[str, Any]:
    """Resolve uisp-<uuid> devices to their UISP IPs and stamp mgmt_ip."""
    from app.services.topology.uisp_ip_backfill import backfill_uisp_mgmt_ips
    from app.services.uisp import UispClient, UispClientError

    db = db_session_adapter.create_session()
    try:
        client = UispClient.from_env()
        result = backfill_uisp_mgmt_ips(db, client, dry_run=dry_run)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        logger.info("uisp_ip_backfill_done dry_run=%s %s", dry_run, result)
        return {"dry_run": dry_run, **result}
    except UispClientError as exc:
        db.rollback()
        logger.warning("uisp_ip_backfill_failed: %s", exc)
        return {"error": "uisp_unavailable", "message": str(exc)}
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("uisp_ip_backfill_timed_out")
        return {"error": "uisp_ip_backfill_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("uisp_ip_backfill_failed")
        return {"error": str(exc)}
    finally:
        db.close()
