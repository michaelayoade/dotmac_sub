"""Scheduled billing service runners.

Celery tasks should be transport wrappers. These runners own the session,
transaction, and logging boundary for scheduled billing automation.
"""

from __future__ import annotations

import logging

from app.services import billing_automation as billing_automation_service
from app.services.billing_enforcement_guards import (
    billing_enforcement_health,
    notification_delivery_health,
)
from app.services.billing_settings import (
    billing_enabled,
    check_billing_switch,
    disabled_billing_components,
)
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


def scheduled_billing_enabled() -> bool:
    """Return whether scheduled local billing automation may run."""
    session = SessionLocal()
    try:
        return billing_enabled(session)
    finally:
        session.close()


def run_invoice_cycle() -> dict[str, int]:
    logger.info("Starting billing invoice cycle")
    session = SessionLocal()
    try:
        result = billing_automation_service.run_invoice_cycle(session)
        processed = result.get("subscriptions_billed", 0)
        errors = result.get("errors", 0)
        logger.info(
            "Billing invoice cycle completed: %d billed, %d invoices created, %d errors",
            processed,
            result.get("invoices_created", 0),
            errors,
        )
        session.commit()
        return {"processed": processed, "errors": errors}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_invoices_overdue() -> dict[str, int]:
    logger.info("Starting overdue invoice detection")
    session = SessionLocal()
    try:
        result = billing_automation_service.mark_overdue_invoices(session)
        logger.info(
            "Overdue detection completed: %d marked",
            result.get("marked_overdue", 0),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_billing_switch_health() -> dict:
    """Config-integrity + billing enforcement health guard."""
    session = SessionLocal()
    try:
        switch = check_billing_switch(session)
        enforcement = billing_enforcement_health(session)
        notification = notification_delivery_health(session)
        try:
            disabled_components = (
                disabled_billing_components(session) if switch["actual"] else []
            )
        except Exception:
            logger.exception("disabled_billing_components check failed")
            disabled_components = []
        result = {
            "ok": bool(switch["ok"]) and enforcement.ok,
            "billing_switch": switch,
            "disabled_billing_components": disabled_components,
            "billing_enforcement_health": {
                "ok": enforcement.ok,
                "reasons": enforcement.reasons,
                "details": enforcement.details,
            },
            "notification_delivery_health": {
                "ok": notification.ok,
                "reasons": notification.reasons,
                "details": notification.details,
            },
        }
        if not switch["ok"]:
            logger.critical(
                "billing_switch_drift: billing_enabled=%s expected=%s; "
                "local billing may act on customers unexpectedly",
                switch["actual"],
                switch["expected"],
            )
        if not enforcement.ok:
            logger.critical(
                "billing_enforcement_health_failed: reasons=%s details=%s",
                ",".join(enforcement.reasons),
                enforcement.details,
            )
        if not notification.ok:
            logger.error(
                "billing_notification_delivery_unhealthy: reasons=%s details=%s",
                ",".join(notification.reasons),
                notification.details,
            )
        if disabled_components:
            logger.critical(
                "billing_component_disabled: billing is live but these capture "
                "components are switched OFF: %s; re-enable them or collections "
                "will silently under-run",
                ",".join(disabled_components),
            )
        _append_billing_health_snapshot(session, result)
        return result
    finally:
        session.close()


def _append_billing_health_snapshot(session, result: dict) -> None:
    try:
        from app.services.billing_health import billing_health_snapshot

        health = billing_health_snapshot(session)
        anomalies = set(health.anomalies)
        result["billing_health"] = {
            "paid_with_balance_count": health.paid_with_balance_count,
            "last_scanned": health.last_scanned,
            "eligible_active_subs": health.eligible_active_subs,
            "scan_ratio": health.scan_ratio,
            "payments_24h": health.payments_24h,
            "payments_7d_daily_avg": health.payments_7d_daily_avg,
            "payment_volume_ratio": health.payment_volume_ratio,
            "stale_runners": health.stale_runners,
            "covered_but_locked": health.covered_but_locked,
            "unbilled_no_path": health.unbilled_no_path,
            "active_subs_on_terminal_account": (health.active_subs_on_terminal_account),
            "negative_prepaid_balance_count": health.negative_prepaid_balance_count,
            "negative_prepaid_balance_total": str(
                health.negative_prepaid_balance_total
            ),
            "prepaid_balance_sweep_enabled": health.prepaid_balance_sweep_enabled,
            "negative_prepaid_with_sweep_disabled_count": (
                health.negative_prepaid_with_sweep_disabled_count
            ),
            "anomalies": sorted(anomalies),
        }
        if "paid_invoices_with_balance" in anomalies:
            logger.error(
                "billing_paid_invoices_with_balance: %d invoices status=paid "
                "carry non-zero balance_due (total %s) - AR-integrity defect",
                health.paid_with_balance_count,
                health.paid_with_balance_total,
            )
        if "invoice_scan_count_low" in anomalies:
            logger.error(
                "billing_invoice_scan_count_low: last run scanned %s of ~%d "
                "eligible subscriptions (ratio %.2f) - cycle may have stopped "
                "scanning a cohort",
                health.last_scanned,
                health.eligible_active_subs,
                health.scan_ratio,
            )
        if "payment_volume_collapse" in anomalies:
            logger.error(
                "billing_payment_volume_collapse: last-24h %d succeeded payments "
                "vs 7d daily avg %.1f (ratio %.2f) - payment intake may be broken",
                health.payments_24h,
                health.payments_7d_daily_avg,
                health.payment_volume_ratio,
            )
        if "runner_heartbeat_stale" in anomalies:
            logger.error(
                "billing_runner_heartbeat_stale: no fresh success for %s - a "
                "billing/collections runner may be stalled or dead",
                ",".join(health.stale_runners),
            )
        if "enforcement_covered_but_locked" in anomalies:
            logger.error(
                "billing_enforcement_covered_but_locked: %d accounts under a "
                "billing lock despite ledger balance >= 0 - wrongful-suspension drift",
                health.covered_but_locked,
            )
        if "active_subs_without_billing_path" in anomalies:
            logger.error(
                "billing_active_subs_without_billing_path: %d active prepaid "
                "subscription(s) no enabled billing path will invoice (flag off "
                "or non-monthly offer) - revenue leak",
                health.unbilled_no_path,
            )
        if "negative_prepaid_balances" in anomalies:
            logger.error(
                "billing_negative_prepaid_balances: %d prepaid account(s) have "
                "wallet balance below zero (total exposure %s)",
                health.negative_prepaid_balance_count,
                health.negative_prepaid_balance_total,
            )
        if "negative_prepaid_sweep_disabled" in anomalies:
            logger.error(
                "billing_negative_prepaid_sweep_disabled: %d negative prepaid "
                "account(s) exist while prepaid_balance_sweep is disabled",
                health.negative_prepaid_with_sweep_disabled_count,
            )
    except Exception:
        logger.exception(
            "billing_health_snapshot_failed: monitoring snapshot raised; "
            "skipping liveness anomaly alerts (enforcement guard unaffected)"
        )


def audit_cutover_balance_invariant() -> dict:
    from app.services.cutover_balance_audit import audit_cutover_balance_invariant

    session = SessionLocal()
    try:
        result = audit_cutover_balance_invariant(session)
        if result.get("ok"):
            logger.info(
                "cutover_balance_invariant_ok: population=%s",
                result.get("population"),
            )
        else:
            logger.error(
                "cutover_balance_invariant_drift: population=%s raw_drift=%s "
                "unregistered_drift=%s overcredited=%s/%s understated=%s/%s "
                "inactive_seed_drift=%s post_adjustment_drift=%s "
                "post_adjustments=%s/%s excluded_adjustments=%s/%s "
                "registered_variance=%s/%s stale_registered_variance=%s",
                result.get("population"),
                result.get("raw_drift_count"),
                result.get("drift_count"),
                result.get("overcredited_count"),
                result.get("overcredited_total"),
                result.get("understated_count"),
                result.get("understated_total"),
                result.get("inactive_seed_drift_count"),
                result.get("post_adjustment_drift_count"),
                result.get("post_adjustment_entry_count"),
                result.get("post_adjustment_net"),
                result.get("excluded_adjustment_entry_count"),
                result.get("excluded_adjustment_net"),
                result.get("registered_variance_count"),
                result.get("registered_variance_total"),
                result.get("stale_registered_variance_count"),
            )
        return result
    finally:
        session.close()


def audit_funded_inactive_exposure() -> dict:
    from app.services.funded_inactive_exposure import funded_inactive_exposure

    session = SessionLocal()
    try:
        result = funded_inactive_exposure(session)
        log_fn = logger.error if result.get("refund_review_count") else logger.info
        log_fn(
            "funded_inactive_exposure: ok=%s inactive_positive=%s/%s "
            "refund_review=%s/%s disabled=%s/%s canceled=%s/%s "
            "suspended=%s/%s blocked=%s/%s soft_deleted=%s/%s "
            "sibling_candidates=%s material=%s",
            result.get("ok"),
            result.get("inactive_positive_count"),
            result.get("inactive_positive_total"),
            result.get("refund_review_count"),
            result.get("refund_review_total"),
            result.get("disabled_count"),
            result.get("disabled_total"),
            result.get("canceled_count"),
            result.get("canceled_total"),
            result.get("suspended_count"),
            result.get("suspended_total"),
            result.get("blocked_count"),
            result.get("blocked_total"),
            result.get("soft_deleted_count"),
            result.get("soft_deleted_total"),
            result.get("sibling_candidate_count"),
            result.get("material_count"),
        )
        return result
    finally:
        session.close()


def run_billing_notifications() -> dict[str, int | bool]:
    logger.info("Starting billing notifications run")
    session = SessionLocal()
    try:
        result = billing_automation_service.run_billing_notifications(session)
        logger.info(
            "Billing notifications run completed: %s reminders, %s escalations%s",
            result.get("invoice_reminders_sent", 0),
            result.get("dunning_escalations_sent", 0),
            " (outside send window)" if result.get("skipped_outside_window") else "",
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
