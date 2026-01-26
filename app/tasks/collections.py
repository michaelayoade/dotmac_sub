from app.celery_app import celery_app
from app.db import SessionLocal
from app.schemas.collections import DunningRunRequest, PrepaidEnforcementRunRequest
from app.services import collections as collections_service


@celery_app.task(name="app.tasks.collections.run_dunning")
def run_dunning():
    session = SessionLocal()
    try:
        collections_service.dunning_workflow.run(session, DunningRunRequest())
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.collections.run_prepaid_enforcement")
def run_prepaid_enforcement():
    session = SessionLocal()
    try:
        collections_service.prepaid_enforcement.run(
            session, PrepaidEnforcementRunRequest()
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
