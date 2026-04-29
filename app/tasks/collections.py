import logging

from app.celery_app import celery_app
from app.schemas.collections import DunningRunRequest, PrepaidEnforcementRunRequest
from app.services import collections as collections_service
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.collections.run_dunning")
def run_dunning() -> dict[str, int]:
    logger.info("Starting dunning run")
    session = SessionLocal()
    try:
        result = collections_service.dunning_workflow.run(session, DunningRunRequest())
        processed = result.get("processed", 0) if isinstance(result, dict) else 0
        errors = result.get("errors", 0) if isinstance(result, dict) else 0
        logger.info("Dunning run completed: processed=%d errors=%d", processed, errors)
        session.commit()
        return {"processed": processed, "errors": errors}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.collections.run_prepaid_enforcement")
def run_prepaid_enforcement() -> dict[str, int]:
    logger.info("Starting prepaid enforcement run")
    session = SessionLocal()
    try:
        result = collections_service.prepaid_enforcement.run(
            session, PrepaidEnforcementRunRequest()
        )
        processed = result.get("processed", 0) if isinstance(result, dict) else 0
        errors = result.get("errors", 0) if isinstance(result, dict) else 0
        logger.info(
            "Prepaid enforcement completed: processed=%d errors=%d", processed, errors
        )
        session.commit()
        return {"processed": processed, "errors": errors}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
