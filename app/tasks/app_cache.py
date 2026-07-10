from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.app_cache.refresh_dashboard_stats_cache",
    soft_time_limit=120,
    time_limit=150,
)
def refresh_dashboard_stats_cache_task() -> dict[str, object]:
    db = db_session_adapter.create_session()
    try:
        from app.services import web_admin_dashboard

        stats = web_admin_dashboard.refresh_dashboard_stats_cache(db)
        return {"refreshed": True, "fields": sorted(stats.keys())}
    except Exception as exc:
        logger.exception("dashboard_stats_cache_task_failed")
        db.rollback()
        return {"refreshed": False, "error": str(exc)}
    finally:
        db.close()
