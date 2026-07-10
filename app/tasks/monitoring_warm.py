"""Background warmer for the per-OLT Zabbix summary cache.

`/admin/network/monitoring` reads a per-OLT Zabbix online/offline *summary*
cache (added with the dashboard perf work). It is only populated when the page
renders, so the first viewer after a cache expiry pays the full per-OLT live
Zabbix summary fan-out (~30s cold). This task keeps that cache hot out-of-band.

It must run below the cache TTL (default 180s) so a viewer never hits a cold
cache. The per-OLT snapshot cache is warmed separately by
``app.tasks.app_cache.refresh_ont_zabbix_snapshot_cache``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


# expires below the 120s schedule interval so a transiently backlogged worker
# drops stale warmers instead of accumulating a queue of them.
@celery_app.task(
    name="app.tasks.monitoring_warm.warm_monitoring_caches",
    expires=110,
    soft_time_limit=90,
    time_limit=110,
)
def warm_monitoring_caches() -> dict[str, Any]:
    """Refresh the per-OLT Zabbix summary cache (ont-zabbix-summary:*)."""
    from app.services.network_monitoring import get_onu_status_summary
    from app.services.zabbix import zabbix_configured

    if not zabbix_configured():
        return {"skipped": "zabbix_token_missing"}

    try:
        with db_session_adapter.session() as db:
            # refresh=True forces a live per-OLT fetch + fresh cache write, so
            # the cache TTL never lapses between warmer runs.
            get_onu_status_summary(db, refresh=True)
            return {"status": "ok"}
    except Exception:
        logger.warning("warm_monitoring_caches_failed", exc_info=True)
        return {"status": "error"}
