import time

from app.celery_app import celery_app
from app.logging import get_logger
from app.models.integration import IntegrationRun, IntegrationRunStatus
from app.services import integration as integration_service
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.observability import record_task_run


@celery_app.task(name="app.tasks.integrations.run_integration_job")
def run_integration_job(
    job_id: str,
    trigger: str = "schedule",
    requested_by: str | None = None,
):
    start = time.monotonic()
    status = "success"
    logger = get_logger(__name__)
    logger.info("INTEGRATION_JOB_START job_id=%s", job_id)
    try:
        with db_session_adapter.session() as session:
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
            if trigger == "schedule" and requested_by is None:
                integration_service.integration_jobs.run(session, job_id)
            else:
                integration_service.integration_jobs.run(
                    session,
                    job_id,
                    trigger=trigger,
                    requested_by=requested_by,
                )
    except Exception:
        status = "error"
        raise
    finally:
        duration = time.monotonic() - start
        record_task_run("integration_job", status=status, duration_seconds=duration)
