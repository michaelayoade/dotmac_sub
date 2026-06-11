"""Scheduled payment reconciliation maintenance tasks."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.celery_app import celery_app
from app.models.billing import TopupIntent
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(name="app.tasks.payment_reconciliation.reconcile_topups")
def reconcile_topups() -> dict[str, int]:
    """Expire stale pending top-up intents.

    Gateway verification is handled synchronously on callback/webhook paths; this
    runner keeps abandoned intents from staying pending forever.
    """
    session = SessionLocal()
    try:
        now = datetime.now(UTC)
        intents = list(
            session.scalars(
                select(TopupIntent)
                .where(TopupIntent.status == "pending")
                .where(TopupIntent.expires_at.is_not(None))
                .where(TopupIntent.expires_at < now)
            ).all()
        )
        for intent in intents:
            intent.status = "expired"
            intent.updated_at = now
        if intents:
            session.commit()
        result = {"expired_topup_intents": len(intents)}
        logger.info("top-up reconciliation complete: %s", result)
        return result
    finally:
        session.close()
