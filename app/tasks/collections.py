import logging

from app.celery_app import celery_app
from app.schemas.collections import DunningRunRequest
from app.services import collections as collections_service
from app.services.billing_settings import billing_enabled
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.collections.run_dunning")
def run_dunning() -> dict[str, int | str]:
    logger.info("Starting dunning run")
    session = SessionLocal()
    try:
        if not billing_enabled(session):
            logger.info("dunning skipped: local billing disabled (billing_enabled)")
            return {"skipped": "billing_disabled"}
        result = collections_service.dunning_workflow.run(session, DunningRunRequest())
        summary: dict[str, int | str] = {
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
def run_prepaid_enforcement() -> dict[str, int | str]:
    """RETIRED. Deposit-based prepaid enforcement suspended paid customers on a
    stale Splynx deposit/derived balance; due-date dunning (run_dunning) is now
    the sole enforcer. This is a no-op kept only so the task name still resolves;
    it never suspends. Obsolete prepaid locks are cleared by
    run_retired_lock_reconcile.
    """
    logger.info("prepaid enforcement is retired; no-op (use due-date dunning)")
    return {"skipped": "prepaid_enforcement_retired"}


@celery_app.task(name="app.tasks.collections.run_retired_lock_reconcile")
def run_retired_lock_reconcile() -> dict[str, int | str]:
    """Resolve enforcement locks from retired reasons (e.g. prepaid) and restore
    service via the normal restore path. Idempotent; no-op once none remain."""
    logger.info("Starting retired-enforcement-lock reconcile")
    session = SessionLocal()
    try:
        summary = collections_service.reconcile_retired_enforcement_locks(session)
        logger.info("retired-lock reconcile completed: %s", summary)
        return {k: int(v) for k, v in summary.items()}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
