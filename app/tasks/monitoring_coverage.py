"""Monitoring-path coverage refresh task.

Recomputes the reachable-CIDR set (from up WireGuard peers) into the cache on a
short interval, so the operational-status reader and the SLA bridge can tell a
real outage from a monitoring blind spot without running ``wg`` on the request
path. Must run in a container that can execute ``wg`` (otherwise it no-ops
safely — coverage stays unloaded and nothing is penalised).
"""

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.monitoring_coverage.refresh_monitoring_coverage")
def refresh_monitoring_coverage() -> dict:
    """Recompute and cache the reachable management CIDRs."""
    from app.services.monitoring_coverage import refresh_coverage_cache

    result = refresh_coverage_cache()
    logger.info("monitoring coverage refreshed: cidrs=%d", result["cidrs"])
    return result
