"""Hourly review of unmatched/ambiguous customer radios.

Scheduled via a ``scheduled_tasks`` row (name ``topology_unmatched_radio_review``,
seeded by migration 211) rather than a hardcoded ``build_beat_schedule`` entry —
the generic enabled-rows loop at the end of the builder picks it up.
"""

import logging
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.unmatched_radio.run_unmatched_radio_review",
    soft_time_limit=240,
    time_limit=300,
)
def run_unmatched_radio_review() -> dict[str, Any]:
    """Open/auto-close ops-queue items for radios that never got matched."""
    db = db_session_adapter.create_session()
    try:
        from app.services import unmatched_radio_queue

        stats = unmatched_radio_queue.evaluate(db)
        db.commit()
        logger.info("unmatched_radio_review_done %s", stats)
        return stats
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("unmatched_radio_review_failed")
        return {"error": str(exc)}
    finally:
        db.close()
