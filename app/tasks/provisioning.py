"""Celery tasks for provisioning workflows."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services import web_provisioning_bulk_activate as bulk_activate_service
from app.services import web_provisioning_migration as migration_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


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


@celery_app.task(name="app.tasks.provisioning.run_coordinated_provisioning_task")
def run_coordinated_provisioning_task(
    operation_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    bundle_id: str | None = None,
    force_reauthorize: bool = False,
):
    """Execute coordinated provisioning workflow.

    This task is queued by provisioning_coordinator.queue_provisioning()
    and executes the complete OLT → ACS provisioning sequence.
    """
    from app.services.network.provisioning_coordinator import ProvisioningCoordinator
    from app.services.network_operations import network_operations

    try:
        with db_session_adapter.session() as session:
            # Mark operation as running
            network_operations.mark_running(
                session,
                operation_id,
                "Executing provisioning workflow...",
            )

            # Execute coordinated provisioning
            coordinator = ProvisioningCoordinator(session)
            result = coordinator.provision_ont(
                olt_id,
                fsp,
                serial_number,
                bundle_id=bundle_id,
                force_reauthorize=force_reauthorize,
            )

            # Update operation with result
            if result.success:
                network_operations.mark_completed(
                    session,
                    operation_id,
                    result.message,
                    output_payload={
                        "ont_id": result.ont_id,
                        "ont_id_on_olt": result.ont_id_on_olt,
                        "duration_ms": result.duration_ms,
                        "steps": result.phase_summary,
                    },
                )
            else:
                network_operations.mark_failed(
                    session,
                    operation_id,
                    result.message,
                    output_payload={
                        "failed_step": (
                            result.failed_step.name if result.failed_step else None
                        ),
                        "steps": result.phase_summary,
                    },
                )

            return {
                "success": result.success,
                "message": result.message,
                "ont_id": result.ont_id,
                "duration_ms": result.duration_ms,
            }
    except Exception as exc:
        logger.error(
            "Coordinated provisioning task failed for %s: %s",
            serial_number,
            exc,
            exc_info=True,
        )
        try:
            with db_session_adapter.session() as session:
                network_operations.mark_failed(
                    session,
                    operation_id,
                    f"Provisioning task failed: {exc}",
                )
        except Exception:
            pass
        raise
