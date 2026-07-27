"""Celery tasks for the sales-to-service lifecycle reconciler.

The reconciler existed before this module but had no scheduled caller, so the
drift it repairs — a funded order with no implementation scope, a verified
installation whose ServiceOrders were never released, an active ServiceOrder
with no CX handoff, a fully funded order whose Subscription push silently
failed — was only ever found when an operator remembered to run
``scripts/migration/reconcile_sales_lifecycle.py`` by hand.

Detect-only by default. Repair creates Subscriptions (and their first
invoice), ServiceOrders and Projects, which is money-adjacent, so apply mode
is a separate registered control
(``projects.sales_lifecycle_reconcile_apply_enabled``) rather than something a
deploy turns on silently.
"""

import logging

from app.celery_app import celery_app
from app.models.domain_settings import SettingDomain
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

#: Counts that mean the sales-to-service contract is currently being violated.
_DRIFT_KEYS = (
    "missing_implementation_scope",
    "funded_orders_missing_subscription",
    "verified_implementation_not_released",
    "verified_implementation_missing_evidence",
    "active_service_orders_without_cx_handoff",
    "unresolvable_offer_lines",
)


def _apply_enabled(db) -> bool:
    """Resolve auto-repair through the one registered control plane.

    Deliberately not an ad-hoc env read: a new scheduler boolean must not
    invent its own environment/database/default precedence path.
    """
    from app.services.settings_spec import resolve_boolean

    return resolve_boolean(
        db, SettingDomain.projects, "sales_lifecycle_reconcile_apply_enabled"
    )


@celery_app.task(name="app.tasks.sales_lifecycle.reconcile_sales_to_service_lifecycle")
def reconcile_sales_to_service_lifecycle(apply: bool | None = None) -> dict:
    """Sweep the sales-to-service chain and report (or repair) drift.

    Returns the reconciler's own counts so the result is inspectable in the
    task log. Detected drift is logged at WARNING with the per-class counts,
    pointing at the owner that fixes each one.
    """
    db = db_session_adapter.create_session()
    try:
        from app.services.sales_lifecycle_reconciliation import (
            reconcile_sales_to_service_lifecycle as reconcile,
        )

        should_apply = _apply_enabled(db) if apply is None else bool(apply)
        result = reconcile(db, apply=should_apply)
        drift = {key: int(result.get(key) or 0) for key in _DRIFT_KEYS}
        total_drift = sum(drift.values())
        if total_drift:
            logger.warning(
                "sales_lifecycle_drift_detected apply=%s total=%s detail=%s",
                should_apply,
                total_drift,
                drift,
            )
        else:
            logger.info("sales_lifecycle_reconcile_clean apply=%s", should_apply)
        return result
    finally:
        # The reconciler owns its transaction: it commits owner-backed repairs,
        # rolls back a detect-only sweep, and rolls back a rejected repair
        # before raising. This adapter only owns the session's lifetime.
        db.close()
