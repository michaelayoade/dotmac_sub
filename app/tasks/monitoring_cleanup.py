"""Celery tasks for monitoring data maintenance and NAS sync."""

from __future__ import annotations

import logging
from typing import SupportsInt, cast

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.monitoring_cleanup.check_stale_infrastructure")
def check_stale_infrastructure() -> dict[str, object]:
    """Check for stale DB transactions and Celery backlog indicators."""
    logger.info("Starting stale infrastructure check")
    with db_session_adapter.read_session() as db:
        from app.services.infrastructure_health import check_all_services

        services = check_all_services(db)

    degraded = [
        {
            "name": service.name,
            "status": service.status,
            "details": service.details,
        }
        for service in services
        if service.status != "up"
    ]
    if degraded:
        logger.warning(
            "Infrastructure stale-health check found degraded services: %s",
            degraded,
        )
    return {
        "status": "degraded" if degraded else "up",
        "degraded": degraded,
        "checked": len(services),
    }


@celery_app.task(name="app.tasks.monitoring_cleanup.sync_nas_to_monitoring")
def sync_nas_to_monitoring() -> dict[str, int]:
    """Sync all active NAS devices into the network monitoring system.

    Creates NetworkDevice records for NAS devices that don't have one,
    links them via network_device_id FK, and copies SNMP/IP config.
    """
    logger.info("Starting NAS → monitoring sync")
    with db_session_adapter.session() as db:
        from app.services.monitoring_metrics import sync_all_nas_to_monitoring

        result = sync_all_nas_to_monitoring(db)
        return result


@celery_app.task(name="app.tasks.monitoring_cleanup.cleanup_old_device_metrics")
def cleanup_old_device_metrics() -> dict[str, int]:
    """Delete device metrics older than the configured retention period.

    Runs daily. Deletes in 10K batches to avoid long locks.

    Returns:
        {deleted: N}
    """
    logger.info("Starting device metrics cleanup")
    with db_session_adapter.session() as db:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        retention_days = 90
        try:
            val = resolve_value(
                db, SettingDomain.network_monitoring, "device_metrics_retention_days"
            )
            if val is not None:
                retention_days = int(cast(str | bytes | SupportsInt, val))
        except (TypeError, ValueError):
            pass

        from app.services.monitoring_metrics import (
            cleanup_old_device_metrics as do_cleanup,
        )

        deleted = do_cleanup(db, retention_days=retention_days)
        return {"deleted": deleted, "retention_days": retention_days}
