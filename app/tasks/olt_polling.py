"""Celery tasks for OLT metrics aggregation."""

from __future__ import annotations

from app.celery_app import celery_app


@celery_app.task(name="app.tasks.olt_polling.poll_all_olt_signals")
def poll_all_olt_signals() -> dict[str, int]:
    """Legacy task - status fetched directly from Zabbix on demand."""
    return {"olts_dispatched": 0}


@celery_app.task(name="app.tasks.olt_polling.poll_olt_signal")
def poll_olt_signal(olt_id: str) -> dict[str, str]:
    """Legacy task - status fetched directly from Zabbix on demand."""
    return {"olt_id": olt_id, "status": "skipped"}


@celery_app.task(name="app.tasks.olt_polling.finalize_olt_polling")
def finalize_olt_polling() -> dict[str, int]:
    """Legacy task - monitoring reads Zabbix directly."""
    return {"metrics_pushed": 0}
