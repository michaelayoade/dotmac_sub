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


@celery_app.task(
    name="app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache",
    soft_time_limit=120,
    time_limit=150,
)
def refresh_ont_zabbix_snapshot_cache_task() -> dict[str, object]:
    from app.services.zabbix import zabbix_configured

    if not zabbix_configured():
        return {"refreshed": False, "skipped": "zabbix_token_missing"}

    db = db_session_adapter.create_session()
    try:
        from app.services import zabbix_ont_status

        result = zabbix_ont_status.refresh_all_olt_snapshots_cache(db)
        return {"refreshed": True, **result}
    except Exception as exc:
        logger.exception("ont_zabbix_snapshot_cache_task_failed")
        db.rollback()
        return {"refreshed": False, "error": str(exc)}
    finally:
        db.close()
