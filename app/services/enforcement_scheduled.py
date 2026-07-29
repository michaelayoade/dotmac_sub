"""Scheduled enforcement service runners."""

from __future__ import annotations

import logging
from uuid import uuid4

from app.services import account_billing_approval
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

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


def reconcile_billing_approval_drift() -> dict[str, int]:
    """Converge active service away from the unapproved-account split state."""
    with db_session_adapter.read_session() as db:
        account_ids = account_billing_approval.find_billing_approval_drift_account_ids(
            db
        )

    stats = {
        "candidates": len(account_ids),
        "disabled": 0,
        "treatment_aligned": 0,
        "unchanged": 0,
        "errors": 0,
    }
    for account_id in account_ids:
        command_id = uuid4()
        context = CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="service:billing_approval_reconciler",
            scope=account_billing_approval.BILLING_APPROVAL_WRITE_SCOPE,
            reason="Repair active service with revoked billing approval",
            idempotency_key=f"billing-approval-reconcile:{account_id}:{command_id}",
        )
        try:
            with db_session_adapter.owner_command_session() as db:
                outcome = account_billing_approval.reconcile_account_billing_approval(
                    db,
                    account_billing_approval.ReconcileAccountBillingApprovalCommand(
                        context=context,
                        account_id=account_id,
                    ),
                )
            stats[outcome.action.value] += 1
        except Exception:
            stats["errors"] += 1
            logger.exception(
                "Billing-approval reconciliation failed for account %s",
                account_id,
            )
    return stats


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
