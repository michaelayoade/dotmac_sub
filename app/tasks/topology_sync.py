"""Scheduled topology reconcile (Phase 1, Task 5).

Pulls Zabbix groups/hosts and reconciles them onto pop_sites + network_devices.
Routed to the ``ingestion`` queue (same home as the monitoring warmer), which
has a live consumer.
"""

from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.zabbix import ZabbixClient, ZabbixClientError, zabbix_configured

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.topology_sync.run_topology_reconcile",
    soft_time_limit=240,
    time_limit=300,
)
def run_topology_reconcile() -> dict[str, Any]:
    """Reconcile Zabbix topology into sub's tables; commit on success."""
    if not zabbix_configured():
        return {"skipped": "zabbix_token_missing"}

    from app.services.topology.zabbix_reconcile import reconcile

    db = db_session_adapter.create_session()
    try:
        client = ZabbixClient.from_env()
        result = reconcile(db, client)
        db.commit()
        return result
    except ZabbixClientError as exc:
        db.rollback()
        logger.warning("topology_reconcile_failed: %s", exc)
        return {"error": "zabbix_unavailable", "message": str(exc)}
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("topology_reconcile_timed_out")
        return {"error": "topology_reconcile_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("topology_reconcile_failed")
        return {"error": str(exc)}
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.topology_sync.warm_topology_status",
    soft_time_limit=110,
    time_limit=150,
)
def warm_topology_status() -> dict[str, Any]:
    """Refresh cached live_status for topology nodes from native poll results."""
    from app.services.topology.live_status import (
        touch_warm_heartbeat,
    )
    from app.services.topology.live_status import (
        warm_topology_status as _warm,
    )

    db = db_session_adapter.create_session()
    try:
        result = _warm(db)
        db.commit()
        # Stamp the heartbeat only after a successful refresh so a stalled/failing
        # warmer ages out and stops good states being trusted (see selfcare).
        touch_warm_heartbeat()
        return result
    except SoftTimeLimitExceeded:
        db.rollback()
        logger.warning("topology_status_warm_timed_out")
        return {"error": "topology_status_warm_timed_out"}
    except Exception as exc:  # noqa: BLE001 - report and roll back
        db.rollback()
        logger.exception("topology_status_warm_failed")
        return {"error": str(exc)}
    finally:
        db.close()
