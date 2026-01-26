from app.celery_app import celery_app
from app.db import SessionLocal
from app.services import radius as radius_service


@celery_app.task(name="app.tasks.radius.run_radius_sync_job")
def run_radius_sync_job(job_id: str):
    session = SessionLocal()
    try:
        radius_service.radius_sync_jobs.run(session, job_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
