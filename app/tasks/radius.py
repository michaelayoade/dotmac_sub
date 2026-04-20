import logging

from app.celery_app import celery_app
from app.services import radius as radius_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.radius.run_radius_sync_job")
def run_radius_sync_job(job_id: str) -> dict[str, int]:
    logger.info("Starting run_radius_sync_job for job_id=%s", job_id)
    with db_session_adapter.session() as session:
        radius_service.radius_sync_jobs.run(session, job_id)
        logger.info("Completed run_radius_sync_job for job_id=%s", job_id)
        return {"processed": 1, "errors": 0}
