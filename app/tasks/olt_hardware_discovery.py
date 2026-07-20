"""Celery task for periodic OLT hardware inventory discovery (degraded).

The Zabbix SNMP source this task consumed was retired with the native
monitoring cutover, so the sweep degrades to the same all-zero result it
already produced at runtime while Zabbix was unconfigured (every OLT was
skipped for lack of a monitoring host).
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_hardware_discovery.discover_all_olt_hardware")
def discover_all_olt_hardware() -> dict[str, int]:
    """Degraded no-op sweep: the SNMP inventory source was retired.

    Returns:
        Statistics dict with olts_scanned, created, updated, errors (all 0).
    """
    logger.info("OLT hardware discovery skipped: SNMP inventory source retired")
    return {"olts_scanned": 0, "created": 0, "updated": 0, "errors": 0}
