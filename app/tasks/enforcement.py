from app.celery_app import celery_app
from app.services import enforcement_scheduled


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
    return enforcement_scheduled.cleanup_subscription_block_sessions(
        subscription_id, reason=reason
    )


@celery_app.task(name="app.tasks.enforcement.reconcile_account_status_drift")
def reconcile_account_status_drift() -> dict[str, int]:
    """Repair safe ``new``/``blocked`` parent drift for all-active services.

    The all-active cohort filter is the guard; mixed-status accounts are
    excluded. Pure service-state - no money writes.
    """
    return enforcement_scheduled.reconcile_account_status_drift()


@celery_app.task(name="app.tasks.enforcement.detect_stale_overdue_locks")
def detect_stale_overdue_locks() -> dict[str, int]:
    """Dry-run detector for stale ``overdue`` enforcement locks - accounts held
    suspended by an overdue lock while they owe NO overdue debt. Deliberately
    apply=False (money-adjacent): it surfaces candidates via a WARNING for an
    operator to review and clear manually, closing the SP-8 gap where the
    backstop only ran when someone typed the command. Writes nothing."""
    return enforcement_scheduled.detect_stale_overdue_locks()
