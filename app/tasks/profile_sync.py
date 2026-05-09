from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.services import web_network_olt_profiles as profile_sync_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.profile_sync.execute_due_profile_sync_tasks")
def execute_due_profile_sync_tasks(*, limit: int = 25) -> dict[str, Any]:
    """Execute approved OLT profile sync tasks whose schedule time has arrived."""
    db = db_session_adapter.create_session()
    try:
        return profile_sync_service.execute_due_profile_sync_tasks(
            db,
            executed_by="profile-sync-worker",
            actor_is_admin=True,
            limit=limit,
        )
    except Exception:
        db.rollback()
        logger.exception("olt_profile_sync_due_task_execution_failed")
        raise
    finally:
        db.close()
