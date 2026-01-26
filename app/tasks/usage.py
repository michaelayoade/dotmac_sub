from app.celery_app import celery_app
from app.db import SessionLocal
from app.schemas.usage import UsageRatingRunRequest
from app.services import usage as usage_service


@celery_app.task(name="app.tasks.usage.run_usage_rating")
def run_usage_rating():
    session = SessionLocal()
    try:
        usage_service.usage_rating_runs.run(session, UsageRatingRunRequest())
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
