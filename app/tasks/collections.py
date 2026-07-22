from app.celery_app import celery_app
from app.services.collections import scheduled as scheduled_collections


@celery_app.task(name="app.tasks.collections.run_billing_enforcement")
def run_billing_enforcement() -> dict[str, int | str]:
    return scheduled_collections.run_billing_enforcement()


@celery_app.task(name="app.tasks.collections.run_bundle_reconcile")
def run_bundle_reconcile() -> dict[str, int]:
    """Converge any divergent bundle members to their anchor's state."""
    return scheduled_collections.run_bundle_reconcile()


@celery_app.task(name="app.tasks.collections.prepaid_balance_sweep")
def prepaid_balance_sweep() -> dict[str, int | str]:
    """Balance/expiry-based prepaid enforcement (arm timers, warn, suspend).

    Permanently scheduled. Account-scoped funding, coverage, quarantine,
    shields, grace, and time-of-day policy decide each consequence.
    """
    return scheduled_collections.run_prepaid_balance_sweep()
