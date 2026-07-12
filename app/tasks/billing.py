import logging
from datetime import UTC, datetime
from typing import cast

from app.celery_app import celery_app
from app.services.billing import scheduled as scheduled_billing
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)


@idempotent_task(
    key_func=lambda: f"billing_cycle:{datetime.now(UTC).strftime('%Y-%m-%d')}"
)
def _run_invoice_cycle_idempotent() -> dict[str, int]:
    return scheduled_billing.run_invoice_cycle()


@celery_app.task(name="app.tasks.billing.run_invoice_cycle")
def run_invoice_cycle() -> dict[str, object]:
    if not scheduled_billing.scheduled_billing_enabled():
        logger.info("billing invoice cycle skipped: local billing disabled")
        return {"skipped": "billing_disabled"}
    return cast(dict[str, object], _run_invoice_cycle_idempotent())


@celery_app.task(name="app.tasks.billing.mark_invoices_overdue")
@idempotent_task(
    key_func=lambda: f"overdue_check:{datetime.now(UTC).strftime('%Y-%m-%d-%H')}"
)
def mark_invoices_overdue() -> dict[str, int]:
    """Hourly task: detect past-due invoices and trigger enforcement."""
    return scheduled_billing.mark_invoices_overdue()


@celery_app.task(name="app.tasks.billing.check_billing_switch")
def check_billing_switch_task() -> dict:
    """Config-integrity + billing enforcement health guard.

    This hourly runner is intentionally independent of the billing master
    switch. If billing is accidentally armed or enforcement/payment intake goes
    unhealthy, the scheduler still emits an operator-visible critical log.
    """
    return scheduled_billing.check_billing_switch_health()


@celery_app.task(name="app.tasks.billing.audit_cutover_balance_invariant")
def audit_cutover_balance_invariant_task() -> dict:
    """Read-only guard for cutover-seeded account balance drift."""
    return scheduled_billing.audit_cutover_balance_invariant()


@celery_app.task(name="app.tasks.billing.audit_funded_inactive_exposure")
def audit_funded_inactive_exposure_task() -> dict:
    """Read-only report for inactive accounts carrying positive balances."""
    return scheduled_billing.audit_funded_inactive_exposure()


@celery_app.task(name="app.tasks.billing.run_billing_notifications")
@idempotent_task(
    key_func=lambda: (
        f"billing_notifications:{datetime.now(UTC).strftime('%Y-%m-%d-%H')}"
    )
)
def run_billing_notifications() -> dict[str, int | bool]:
    """Hourly task: emit invoice reminders + dunning escalations within the
    configured send window (no-op outside it). Enable via
    ``collections.billing_notifications_hourly_enabled``."""
    return scheduled_billing.run_billing_notifications()
