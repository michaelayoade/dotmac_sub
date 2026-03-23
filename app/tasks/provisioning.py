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


@celery_app.task(name="app.tasks.provisioning.provision_ont")
def provision_ont(
    *,
    ont_id: str,
    profile_id: str,
    dry_run: bool = False,
    tr069_olt_profile_id: int | None = None,
) -> dict:
    """Run end-to-end ONT provisioning as a background task.

    Args:
        ont_id: OntUnit ID.
        profile_id: OntProvisioningProfile ID.
        dry_run: If True, generate commands without executing.
        tr069_olt_profile_id: OLT-level TR-069 server profile ID.

    Returns:
        ProvisioningJobResult as dict.
    """
    from app.services.network.ont_provisioning_orchestrator import (
        OntProvisioningOrchestrator,
    )

    session = SessionLocal()
    try:
        result = OntProvisioningOrchestrator.provision_ont(
            session,
            ont_id,
            profile_id,
            dry_run=dry_run,
            tr069_olt_profile_id=tr069_olt_profile_id,
        )
        session.commit()
        logger.info("Provisioning task for ONT %s: %s", ont_id, result.message)
        return result.to_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
