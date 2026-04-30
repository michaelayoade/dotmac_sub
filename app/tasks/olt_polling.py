"""Celery tasks for OLT metrics aggregation.

NOTE: OLT SNMP polling and ONT status detection has been moved to Zabbix.
Status is now updated directly via zabbix_data_ingest which handles
online/offline detection from SNMP walk data.
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.olt_polling.poll_all_olt_signals")
def poll_all_olt_signals() -> dict[str, int]:
    """Legacy task - status detection now handled by Zabbix ingest."""
    logger.debug("poll_all_olt_signals: no-op, status handled by Zabbix ingest")
    return {"olts_dispatched": 0}


@celery_app.task(name="app.tasks.olt_polling.poll_olt_signal")
def poll_olt_signal(olt_id: str) -> dict[str, str]:
    """Legacy task - status detection now handled by Zabbix ingest."""
    return {"olt_id": olt_id, "status": "skipped_zabbix_managed"}


@celery_app.task(name="app.tasks.olt_polling.finalize_olt_polling")
def finalize_olt_polling() -> dict[str, int]:
    """Push aggregated ONU/signal metrics to VictoriaMetrics."""
    logger.info("Pushing ONU/signal metrics to VictoriaMetrics")
    with db_session_adapter.read_session() as db:
        try:
            from app.services.monitoring_metrics import push_onu_status_metrics
            from app.services.network_monitoring import get_onu_status_summary

            onu = get_onu_status_summary(db)
            push_onu_status_metrics(
                online=onu.get("online", 0),
                offline=onu.get("offline", 0),
                low_signal=onu.get("low_signal", 0),
            )
            logger.info("Pushed ONU status metrics: %s", onu)
        except Exception as exc:
            logger.warning("Failed to push ONU metrics to VictoriaMetrics: %s", exc)

        try:
            from app.services.network.olt_polling_metrics import _push_signal_metrics

            metrics_count = _push_signal_metrics(db)
            logger.info("Pushed %d signal metrics to VictoriaMetrics", metrics_count)
            return {"metrics_pushed": metrics_count}
        except Exception as e:
            logger.error("Signal metrics push failed: %s", e)
            return {"metrics_pushed": 0}
