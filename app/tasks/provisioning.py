"""Celery tasks for provisioning workflows."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import web_provisioning_bulk_activate as bulk_activate_service
from app.services import web_provisioning_migration as migration_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.provisioning.run_bulk_activation_job")
def run_bulk_activation_job(*, job_id: str):
    session = SessionLocal()
    try:
        return bulk_activate_service.execute_job(session, job_id=job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.provisioning.run_service_migration_job")
def run_service_migration_job(*, job_id: str):
    session = SessionLocal()
    try:
        return migration_service.execute_job(session, job_id=job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.provisioning.run_coordinated_provisioning_task")
def run_coordinated_provisioning_task(
    operation_id: str,
    olt_id: str,
    fsp: str,
    serial_number: str,
    *,
    profile_id: str | None = None,
    force_reauthorize: bool = False,
):
    """Execute coordinated provisioning workflow.

    This task is queued by provisioning_coordinator.queue_provisioning()
    and executes the complete OLT → ACS provisioning sequence.
    """
    from app.services.network.provisioning_coordinator import ProvisioningCoordinator
    from app.services.network_operations import network_operations

    session = SessionLocal()
    try:
        # Mark operation as running
        network_operations.mark_running(
            session,
            operation_id,
            "Executing provisioning workflow...",
        )
        session.commit()

        # Execute coordinated provisioning
        coordinator = ProvisioningCoordinator(session)
        result = coordinator.provision_ont(
            olt_id,
            fsp,
            serial_number,
            profile_id=profile_id,
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
                    "failed_step": result.failed_step.name if result.failed_step else None,
                    "steps": result.phase_summary,
                },
            )

        session.commit()
        return {
            "success": result.success,
            "message": result.message,
            "ont_id": result.ont_id,
            "duration_ms": result.duration_ms,
        }

    except Exception as exc:
        session.rollback()
        logger.error(
            "Coordinated provisioning task failed for %s: %s",
            serial_number,
            exc,
            exc_info=True,
        )
        try:
            network_operations.mark_failed(
                session,
                operation_id,
                f"Provisioning task failed: {exc}",
            )
            session.commit()
        except Exception:
            pass
        raise
    finally:
        session.close()
