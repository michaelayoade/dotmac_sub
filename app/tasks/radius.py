import logging

from app.celery_app import celery_app
from app.services import radius as radius_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.radius.run_radius_sync_job")
def run_radius_sync_job(job_id: str) -> dict[str, int]:
    logger.info("Starting run_radius_sync_job for job_id=%s", job_id)
    session = SessionLocal()
    try:
        radius_service.radius_sync_jobs.run(session, job_id)
        logger.info("Completed run_radius_sync_job for job_id=%s", job_id)
        session.commit()
        return {"processed": 1, "errors": 0}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.radius.audit_suspension_enforcement")
def audit_suspension_enforcement() -> dict:
    """Periodic read-only check that fully-blocked subscribers are actually
    unreachable in the external RADIUS DB. Logs a warning and bumps the
    radius_suspension_audit_leaks gauge per leak class — drift here used to
    accumulate invisibly (suspended subscribers staying online)."""
    from app.metrics import RADIUS_SUSPENSION_AUDIT_LEAKS
    from app.services.radius_reconciliation import (
        audit_suspension_enforcement as run_audit,
    )

    session = SessionLocal()
    try:
        result = run_audit(session)
        for kind, count in result["counts"].items():
            RADIUS_SUSPENSION_AUDIT_LEAKS.labels(kind=kind).set(count)
        RADIUS_SUSPENSION_AUDIT_LEAKS.labels(kind="mixed_status_subscribers").set(
            result["mixed_status_subscribers"]
        )
        return result
    finally:
        session.close()
