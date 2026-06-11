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
        summary = {
            "accounts_scanned": int(result.accounts_scanned),
            "cases_created": int(result.cases_created),
            "actions_created": int(result.actions_created),
            "skipped": int(result.skipped),
        }
        logger.info(
            "Dunning run completed: accounts_scanned=%d cases_created=%d "
            "actions_created=%d skipped=%d",
            summary["accounts_scanned"],
            summary["cases_created"],
            summary["actions_created"],
            summary["skipped"],
        )
        session.commit()
        return summary
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
        summary = {
            "accounts_scanned": int(result.accounts_scanned),
            "accounts_warned": int(result.accounts_warned),
            "accounts_suspended": int(result.accounts_suspended),
            "accounts_deactivated": int(result.accounts_deactivated),
            "skipped": int(result.skipped),
        }
        logger.info(
            "Prepaid enforcement completed: accounts_scanned=%d accounts_warned=%d "
            "accounts_suspended=%d accounts_deactivated=%d skipped=%d",
            summary["accounts_scanned"],
            summary["accounts_warned"],
            summary["accounts_suspended"],
            summary["accounts_deactivated"],
            summary["skipped"],
        )
        session.commit()
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
