"""Scheduled payment reconciliation maintenance tasks."""

from __future__ import annotations

from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.payment_reconciliation import (
    RECONCILIATION_SCOPE,
    RunTopupReconciliationCommand,
    reconcile_pending_topups,
)


@celery_app.task(name="app.tasks.payment_reconciliation.reconcile_topups")
def reconcile_topups() -> dict[str, int]:
    """Sweep stranded top-up intents against the gateway verify API."""

    observed_at = datetime.now(UTC)
    with db_session_adapter.owner_command_session() as db:
        result = reconcile_pending_topups(
            db,
            RunTopupReconciliationCommand(observed_at=observed_at),
            context=CommandContext.system(
                actor="task:payment-reconciliation",
                scope=RECONCILIATION_SCOPE,
                reason="Run scheduled stranded top-up reconciliation",
            ),
        )
    return result.as_dict()
