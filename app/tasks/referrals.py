"""Referral lifecycle driver and tombstones for retired CRM mirror tasks.

The task names remain registered so already queued messages are absorbed safely
during rollout. They perform no database or network work; Sub owns referral
reads and writes natively.
"""

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.referrals.expire_pending_referrals")
def expire_pending_referrals() -> dict[str, int]:
    from app.services.owner_commands import CommandContext
    from app.services.referrals import (
        ExpirePendingReferralsCommand,
        REFERRAL_PROGRAM_SCOPE,
        expire_pending_referrals as expire_pending,
    )

    with db_session_adapter.owner_command_session() as db:
        count = expire_pending(
            db,
            ExpirePendingReferralsCommand(
                context=CommandContext.system(
                    actor="task:referral-expiry",
                    scope=REFERRAL_PROGRAM_SCOPE,
                    reason="Expire pending referrals beyond the qualification window",
                ),
            ),
        )
    logger.info("pending_referrals_expired", extra={"expired": count})
    return {"expired": count}


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
