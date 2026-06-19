"""Celery tasks for non-ONT provisioning workflows."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import web_provisioning_bulk_activate as bulk_activate_service
from app.services import web_provisioning_migration as migration_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.provisioning.reap_stale_provisioning_runs")
def reap_stale_provisioning_runs(*, older_than_minutes: int = 30) -> dict[str, int]:
    """Fail provisioning runs stuck in 'running' past a timeout.

    A synchronous ``ProvisioningRuns.run`` whose worker died mid-loop leaves
    the row ``running`` forever. This beat task converges those to ``failed``.
    """
    from app.services.provisioning_managers import ProvisioningRuns

    with db_session_adapter.session() as session:
        reaped = ProvisioningRuns.reap_stale_runs(
            session, older_than_minutes=older_than_minutes
        )
    return {"reaped": reaped}


@celery_app.task(name="app.tasks.provisioning.run_bulk_activation_job")
def run_bulk_activation_job(*, job_id: str):
    with db_session_adapter.session() as session:
        return bulk_activate_service.execute_job(session, job_id=job_id)


@celery_app.task(name="app.tasks.provisioning.run_service_migration_job")
def run_service_migration_job(*, job_id: str):
    with db_session_adapter.session() as session:
        return migration_service.execute_job(session, job_id=job_id)


@celery_app.task(name="app.tasks.provisioning.retry_pending_compensation_failures")
def retry_pending_compensation_failures(*, limit: int = 20):
    """Retry due compensation-failure rows with exponential backoff."""
    from app.services.network.compensation_retry import retry_due_compensations

    logger.info(
        "Starting compensation failure watchdog",
        extra={"event": "compensation_retry_watchdog_start", "limit": limit},
    )
    with db_session_adapter.session() as session:
        result = retry_due_compensations(
            session,
            limit=limit,
            resolved_by="system:watchdog",
        )
        logger.info(
            "Compensation failure watchdog completed",
            extra={
                "event": "compensation_retry_watchdog_complete",
                "due_count": result.get("due_count"),
                "retried": result.get("retried"),
                "resolved": result.get("resolved"),
                "still_pending": result.get("still_pending"),
            },
        )
        return result
