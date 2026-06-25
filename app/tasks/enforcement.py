import logging

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@celery_app.task(
    name="app.tasks.enforcement.cleanup_subscription_block_sessions",
    soft_time_limit=30,
    time_limit=45,
)
def cleanup_subscription_block_sessions(
    subscription_id: str, reason: str = "blocked"
) -> dict[str, int]:
    """Disconnect active sessions and apply the NAS-side block out of band.

    FUP/billing enforcement must commit the authoritative DB/RADIUS state even
    if a NAS is slow or unavailable. The periodic safety net can re-converge,
    but this task keeps the customer-facing session cleanup prompt.
    """
    from app.services.enforcement import (
        apply_subscription_address_list_block,
        disconnect_subscription_sessions,
    )

    session = SessionLocal()
    try:
        disconnected = disconnect_subscription_sessions(
            session, subscription_id, reason=reason
        )
        blocked = apply_subscription_address_list_block(session, subscription_id)
        session.commit()
        return {
            "sessions_disconnected": int(disconnected or 0),
            "address_list_blocks": int(blocked or 0),
        }
    except Exception:
        session.rollback()
        logger.exception(
            "subscription_block_session_cleanup_failed",
            extra={
                "event": "subscription_block_session_cleanup_failed",
                "subscription_id": subscription_id,
                "reason": reason,
            },
        )
        raise
    finally:
        session.close()
