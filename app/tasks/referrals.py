"""Celery tasks for the local referral mirror (RFC #73)."""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.referrals.reconcile_referral_mirror")
def reconcile_referral_mirror() -> dict[str, int]:
    """Reconcile stale local referral mirrors against the CRM (the backstop for
    missed webhook deliveries). Returns {reconciled: N}."""
    logger.info("Starting reconcile_referral_mirror")
    db = db_session_adapter.create_session()
    try:
        from app.services import referrals_mirror

        count = referrals_mirror.reconcile_all(db)
        logger.info("Completed reconcile_referral_mirror: reconciled=%s", count)
        return {"reconciled": count}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
