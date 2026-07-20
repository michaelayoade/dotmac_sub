"""Scheduled payment reconciliation maintenance tasks."""

from __future__ import annotations

from app.celery_app import celery_app
from app.services.payment_reconciliation import reconcile_topups_scheduled


@celery_app.task(name="app.tasks.payment_reconciliation.reconcile_topups")
def reconcile_topups() -> dict[str, int]:
    """Sweep stranded top-up intents against the gateway verify API."""
    return reconcile_topups_scheduled()
