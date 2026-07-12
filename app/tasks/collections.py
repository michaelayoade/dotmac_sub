from app.celery_app import celery_app
from app.services.collections import scheduled as scheduled_collections


@celery_app.task(name="app.tasks.collections.run_billing_enforcement")
def run_billing_enforcement() -> dict[str, int | str]:
    return scheduled_collections.run_billing_enforcement()


@celery_app.task(name="app.tasks.collections.run_bundle_reconcile")
def run_bundle_reconcile() -> dict[str, int]:
    """Converge any divergent bundle members to their anchor's state."""
    return scheduled_collections.run_bundle_reconcile()


@celery_app.task(name="app.tasks.collections.run_dunning")
def run_dunning() -> dict[str, int | str]:
    """Backward-compatible task alias for the unified billing enforcer."""
    return run_billing_enforcement()


@celery_app.task(name="app.tasks.collections.prepaid_balance_sweep")
def prepaid_balance_sweep() -> dict[str, int | str]:
    """Balance/expiry-based prepaid enforcement (arm timers, warn, suspend).

    Gated OFF by default behind the ``collections.prepaid_balance_enforcement``
    control; the service returns ``{"skipped": "disabled"}`` when the control is
    off, so the beat entry firing is harmless until an operator opts in.
    """
    return scheduled_collections.run_prepaid_balance_sweep()
