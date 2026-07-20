"""Tombstones for retired outbound CRM referral-mirror tasks.

The task names remain registered so already queued messages are absorbed safely
during rollout. They perform no database or network work; Sub owns referral
reads and writes natively.
"""

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.referrals.reconcile_referral_mirror")
def reconcile_referral_mirror() -> dict[str, int]:
    """Absorb a legacy scheduled message without contacting CRM."""
    logger.info("referral_mirror_reconcile_retired")
    return {"reconciled": 0}


@celery_app.task(name="app.tasks.referrals.refresh_referral_mirror_for_subscriber")
def refresh_referral_mirror_for_subscriber(subscriber_id: str) -> dict[str, bool]:
    """Absorb a legacy per-subscriber refresh without contacting CRM."""
    logger.info("referral_mirror_refresh_retired subscriber=%s", subscriber_id)
    return {"refreshed": False}
