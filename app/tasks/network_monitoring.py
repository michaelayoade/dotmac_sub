"""Celery tasks for periodic network monitoring health refresh."""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.network_monitoring.refresh_core_device_ping")
def refresh_core_device_ping() -> dict[str, int]:
    """Legacy app-side ping refresh kept as a no-op for old schedules."""
    logger.info("Skipping core-device ping refresh: monitoring is managed by Zabbix")
    return {"skipped": 1}


@celery_app.task(name="app.tasks.network_monitoring.refresh_core_device_snmp")
def refresh_core_device_snmp() -> dict[str, int]:
    """Legacy app-side SNMP refresh kept as a no-op for old schedules."""
    logger.info("Skipping core-device SNMP refresh: monitoring is managed by Zabbix")
    return {"skipped": 1}
