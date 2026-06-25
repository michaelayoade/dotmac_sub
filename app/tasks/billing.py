import logging
from datetime import UTC, datetime

from app.celery_app import celery_app
from app.services import billing_automation as billing_automation_service
from app.services.billing_enforcement_guards import (
    billing_enforcement_health,
    notification_delivery_health,
)
from app.services.billing_settings import billing_enabled, check_billing_switch
from app.services.db_session_adapter import db_session_adapter
from app.services.task_idempotency import idempotent_task

logger = logging.getLogger(__name__)
SessionLocal = db_session_adapter.create_session


@idempotent_task(
    key_func=lambda: f"billing_cycle:{datetime.now(UTC).strftime('%Y-%m-%d')}"
)
def _run_invoice_cycle_idempotent() -> dict[str, int]:
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


@celery_app.task(name="app.tasks.billing.run_invoice_cycle")
def run_invoice_cycle() -> dict[str, int | str]:
    session = SessionLocal()
    try:
        if not billing_enabled(session):
            logger.info("billing invoice cycle skipped: local billing disabled")
            return {"skipped": "billing_disabled"}
    finally:
        session.close()
    return _run_invoice_cycle_idempotent()


@celery_app.task(name="app.tasks.billing.mark_invoices_overdue")
@idempotent_task(
    key_func=lambda: f"overdue_check:{datetime.now(UTC).strftime('%Y-%m-%d-%H')}"
)
def mark_invoices_overdue() -> dict[str, int]:
    """Hourly task: detect past-due invoices and trigger enforcement."""
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


@celery_app.task(name="app.tasks.billing.check_billing_switch")
def check_billing_switch_task() -> dict:
    """Config-integrity + billing enforcement health guard.

    This hourly runner is intentionally independent of the billing master
    switch. If billing is accidentally armed or enforcement/payment intake goes
    unhealthy, the scheduler still emits an operator-visible critical log.
    """
    session = SessionLocal()
    try:
        switch = check_billing_switch(session)
        enforcement = billing_enforcement_health(session)
        notification = notification_delivery_health(session)
        result = {
            "ok": bool(switch["ok"]) and enforcement.ok,
            "billing_switch": switch,
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

        # Billing liveness/anomaly monitoring (alert-only; never blocks
        # enforcement). logger.error so each surfaces as an operator page via
        # GlitchTip; the same signals are exported as Prometheus gauges.
        #
        # Failure isolation: this is MONITORING, not a gate. A snapshot error
        # (bad query, schema drift, DB hiccup) must never crash the hourly
        # billing guard or mask the config/enforcement/notification alerts
        # emitted above. Swallow + log.exception so the task always completes.
        # TODO: add per-anomaly alert cooldown to avoid paging every hour.
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
                "active_subs_on_terminal_account": (
                    health.active_subs_on_terminal_account
                ),
                "anomalies": sorted(anomalies),
            }
            if "paid_invoices_with_balance" in anomalies:
                logger.error(
                    "billing_paid_invoices_with_balance: %d invoices status=paid "
                    "carry non-zero balance_due (total %s) — AR-integrity defect",
                    health.paid_with_balance_count,
                    health.paid_with_balance_total,
                )
            if "invoice_scan_count_low" in anomalies:
                logger.error(
                    "billing_invoice_scan_count_low: last run scanned %s of ~%d "
                    "eligible subscriptions (ratio %.2f) — cycle may have stopped "
                    "scanning a cohort",
                    health.last_scanned,
                    health.eligible_active_subs,
                    health.scan_ratio,
                )
            if "payment_volume_collapse" in anomalies:
                logger.error(
                    "billing_payment_volume_collapse: last-24h %d succeeded payments "
                    "vs 7d daily avg %.1f (ratio %.2f) — payment intake may be broken",
                    health.payments_24h,
                    health.payments_7d_daily_avg,
                    health.payment_volume_ratio,
                )
            if "runner_heartbeat_stale" in anomalies:
                logger.error(
                    "billing_runner_heartbeat_stale: no fresh success for %s — a "
                    "billing/collections runner may be stalled or dead",
                    ",".join(health.stale_runners),
                )
            if "enforcement_covered_but_locked" in anomalies:
                logger.error(
                    "billing_enforcement_covered_but_locked: %d accounts under a "
                    "billing lock despite ledger balance >= 0 — wrongful-suspension "
                    "drift",
                    health.covered_but_locked,
                )
            if "active_subs_without_billing_path" in anomalies:
                logger.error(
                    "billing_active_subs_without_billing_path: %d active prepaid "
                    "subscription(s) no enabled billing path will invoice (flag off "
                    "or non-monthly offer) — revenue leak",
                    health.unbilled_no_path,
                )
        except Exception:
            logger.exception(
                "billing_health_snapshot_failed: monitoring snapshot raised; "
                "skipping liveness anomaly alerts (enforcement guard unaffected)"
            )
        return result
    finally:
        session.close()


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
