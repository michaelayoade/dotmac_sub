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


@celery_app.task(name="app.tasks.enforcement.reconcile_account_status_drift")
def reconcile_account_status_drift() -> dict[str, int]:
    """Repair subscriber-level ``blocked`` drift: subscribers walled-gardened at
    the BNG while ALL their subscriptions are active (a stale account flag that
    ``compute_account_status`` would derive as active). Applies the fix, then
    rebuilds RADIUS once and CoA-kicks the affected sessions. The all-active
    cohort filter is the guard; mixed-status accounts are excluded. Pure
    service-state — no money writes. Beat-rerun self-heals on the next pass."""
    db = SessionLocal()
    try:
        from app.services.account_status_reconcile import reconcile_cohort

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


@celery_app.task(name="app.tasks.enforcement.detect_stale_overdue_locks")
def detect_stale_overdue_locks() -> dict[str, int]:
    """Dry-run detector for stale ``overdue`` enforcement locks — accounts held
    suspended by an overdue lock while they owe NO overdue debt. Deliberately
    apply=False (money-adjacent): it surfaces candidates via a WARNING for an
    operator to review and clear manually, closing the SP-8 gap where the
    backstop only ran when someone typed the command. Writes nothing."""
    db = SessionLocal()
    try:
        from app.services.stale_overdue_lock_reconcile import reconcile

        result = reconcile(db, apply=False)
        if result.candidates:
            logger.warning(
                "detect_stale_overdue_locks found %s stale overdue lock(s) "
                "(dry-run — clear manually after review): would_restore=%s "
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
