"""Celery tasks for native AI operations housekeeping."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.ai_operations.expire_stale_insights")
def expire_stale_insights(*, limit: int = 500) -> dict[str, int]:
    from app.services import ai_operations

    with db_session_adapter.session() as session:
        expired = ai_operations.expire_stale_insights(session, limit=limit)
        session.commit()
        payload = {"expired": expired}
        logger.info(
            "stale AI insights expired",
            extra={"event": "ai_insights_expired", **payload},
        )
        return payload
