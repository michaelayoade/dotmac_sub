import logging

from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import radius as radius_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.radius.run_radius_sync_job")
def run_radius_sync_job(job_id: str) -> dict[str, int]:
    logger.info("Starting run_radius_sync_job for job_id=%s", job_id)
    session = SessionLocal()
    try:
        radius_service.radius_sync_jobs.run(session, job_id)
        logger.info("Completed run_radius_sync_job for job_id=%s", job_id)
        return {"processed": 1, "errors": 0}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
