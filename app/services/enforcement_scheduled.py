"""Scheduled enforcement service runners."""

from __future__ import annotations

import logging

from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


def cleanup_subscription_block_sessions(
    subscription_id: str, reason: str = "blocked"
) -> dict[str, int]:
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


def reconcile_account_status_drift() -> dict[str, int]:
    from app.services.account_status_reconcile import reconcile_cohort

    db = SessionLocal()
    try:
        summary = reconcile_cohort(db, dry_run=False)
        db.commit()
        logger.info(
            "reconcile_account_status_drift candidates=%s changed=%s errors=%s "
            "radius_refreshed=%s sessions_kicked=%s",
            summary.candidates,
            summary.changed,
            summary.errors,
            summary.radius_refreshed,
            summary.sessions_kicked,
        )
        return {
            "candidates": summary.candidates,
            "changed": summary.changed,
            "errors": summary.errors,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def detect_stale_overdue_locks() -> dict[str, int]:
    from app.services.stale_overdue_lock_reconcile import reconcile

    db = SessionLocal()
    try:
        result = reconcile(db, apply=False)
        if result.candidates:
            logger.warning(
                "detect_stale_overdue_locks found %s stale overdue lock(s) "
                "(dry-run - clear manually after review): would_restore=%s "
                "would_clear_only=%s skipped=%s",
                result.candidates,
                result.restored,
                result.lock_cleared_only,
                result.skipped,
            )
        else:
            logger.info("detect_stale_overdue_locks: no stale overdue locks")
        return {"candidates": result.candidates, "applied": 0}
    finally:
        db.close()
