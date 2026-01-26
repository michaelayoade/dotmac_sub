import time

from app.celery_app import celery_app
from app.db import SessionLocal
from app.logging import get_logger
from app.metrics import observe_job
from app.models.integration import IntegrationRun, IntegrationRunStatus
from app.services import integration as integration_service
from app.services.common import coerce_uuid


@celery_app.task(name="app.tasks.integrations.run_integration_job")
def run_integration_job(job_id: str):
    start = time.monotonic()
    status = "success"
    session = SessionLocal()
    logger = get_logger(__name__)
    logger.info("INTEGRATION_JOB_START job_id=%s", job_id)
    try:
        running = (
            session.query(IntegrationRun.id)
            .filter(IntegrationRun.job_id == coerce_uuid(job_id))
            .filter(IntegrationRun.status == IntegrationRunStatus.running)
            .first()
        )
        if running:
            status = "skipped"
            logger.info("integration_job_skipped_running job_id=%s", job_id)
            return
        integration_service.integration_jobs.run(session, job_id)
    except Exception:
        status = "error"
        session.rollback()
        raise
    finally:
        session.close()
        duration = time.monotonic() - start
        observe_job("integration_job", status, duration)
