"""Celery tasks for native team inbox operations."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.team_inbox.retry_failed_outbound_messages")
def retry_failed_outbound_messages(
    *,
    limit: int = 50,
    max_retry_count: int = 5,
) -> dict[str, int]:
    from app.services import team_inbox_operations

    with db_session_adapter.session() as session:
        result = team_inbox_operations.retry_failed_outbound_batch(
            session,
            limit=limit,
            max_retry_count=max_retry_count,
        )
        session.commit()
        retried = result.get("retried")
        skipped = result.get("skipped")
        payload = {
            "retried": len(retried) if isinstance(retried, list) else 0,
            "skipped": len(skipped) if isinstance(skipped, list) else 0,
        }
        logger.info(
            "team inbox failed outbound retry complete",
            extra={"event": "team_inbox_failed_outbound_retry", **payload},
        )
        return payload


@celery_app.task(name="app.tasks.team_inbox.promote_message_media_assets")
def promote_message_media_assets(*, limit: int = 200) -> dict[str, int]:
    from app.services import team_inbox_media

    with db_session_adapter.session() as session:
        promoted = team_inbox_media.promote_unmaterialized_assets(
            session,
            limit=limit,
        )
        session.commit()
        payload = {"promoted": promoted}
        logger.info(
            "team inbox media asset promotion complete",
            extra={"event": "team_inbox_media_asset_promotion", **payload},
        )
        return payload
