"""Celery task for evaluating alert rules against device metrics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertStatus,
    DeviceMetric,
)
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

_OPERATORS = {
    AlertOperator.gt: lambda v, t: v > t,
    AlertOperator.gte: lambda v, t: v >= t,
    AlertOperator.lt: lambda v, t: v < t,
    AlertOperator.lte: lambda v, t: v <= t,
    AlertOperator.eq: lambda v, t: abs(v - t) < 0.001,
}


@celery_app.task(name="app.tasks.alert_evaluation.evaluate_alert_rules")
def evaluate_alert_rules() -> dict[str, int]:
    """Evaluate all active alert rules against latest device metrics.

    For each rule:
    1. Fetch the latest DeviceMetric matching the rule's scope
    2. Evaluate the threshold condition
    3. Create or auto-resolve alerts as needed

    Returns:
        Statistics dict with rules_checked, alerts_created, alerts_resolved.
    """
    logger.info("Starting alert rule evaluation")
    rules_checked = 0
    alerts_created = 0
    alerts_resolved = 0

    with db_session_adapter.session() as db:
        rules = list(
            db.scalars(select(AlertRule).where(AlertRule.is_active.is_(True))).all()
        )

        errors = 0
        for rule in rules:
            rules_checked += 1
            try:
                with db.begin_nested():  # savepoint — isolates per-rule
                    created, resolved = _evaluate_rule(db, rule)
                    alerts_created += created
                    alerts_resolved += resolved
            except OperationalError:
                logger.exception(
                    "DB connection error evaluating rule %s — aborting", rule.id
                )
                errors += 1
                break  # Session is dead, no point continuing
            except Exception:
                logger.exception("Error evaluating alert rule %s", rule.id)
                errors += 1

        stats = {
            "rules_checked": rules_checked,
            "alerts_created": alerts_created,
            "alerts_resolved": alerts_resolved,
            "errors": errors,
        }
        logger.info("Alert rule evaluation complete: %s", stats)
        return stats


def _notify_alert(
    db: Session,
    alert: Alert,
    rule: AlertRule,
    metric: DeviceMetric,
    *,
    action: str,
) -> None:
    """Queue alert notifications via policy routing, with admin fallback."""
    try:
        from app.models.network_monitoring import NetworkDevice
        from app.models.notification import (
            Notification,
            NotificationChannel,
            NotificationStatus,
        )
        from app.services import notification as notification_service

        device = db.get(NetworkDevice, metric.device_id) if metric.device_id else None
        device_name = device.name if device else str(metric.device_id or "Unknown")

        status = AlertStatus.open if action == "triggered" else AlertStatus.resolved
        emitted = notification_service.alert_notification_policies.emit_for_alert(
            db,
            alert,
            status,
        )
        if emitted:
            logger.info(
                "Queued %d policy-based alert notification(s) for %s",
                emitted,
                rule.name,
            )
            return

        severity_label = rule.severity.value if rule.severity else "info"
        if action == "triggered":
            subject = f"[{severity_label.upper()}] Alert: {rule.name} on {device_name}"
            body = (
                f"<h2>Alert Triggered</h2>"
                f"<p><strong>Rule:</strong> {rule.name}</p>"
                f"<p><strong>Device:</strong> {device_name}</p>"
                f"<p><strong>Metric:</strong> {rule.metric_type.value if rule.metric_type else 'N/A'}</p>"
                f"<p><strong>Value:</strong> {metric.value:.2f} (threshold: {rule.operator.value} {rule.threshold:.2f})</p>"
                f"<p><strong>Severity:</strong> {severity_label}</p>"
                f"<p><strong>Time:</strong> {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
            )
        else:
            subject = f"[RESOLVED] {rule.name} on {device_name}"
            body = (
                f"<h2>Alert Resolved</h2>"
                f"<p><strong>Rule:</strong> {rule.name}</p>"
                f"<p><strong>Device:</strong> {device_name}</p>"
                f"<p><strong>Current value:</strong> {metric.value:.2f}</p>"
                f"<p><strong>Time:</strong> {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}</p>"
            )

        # Get alert recipients from policy or fallback to admin emails
        recipients = _get_alert_recipients(db, rule)
        for recipient in recipients:
            notification = Notification(
                channel=NotificationChannel.email,
                recipient=recipient,
                subject=subject,
                body=body,
                status=NotificationStatus.queued,
            )
            db.add(notification)

        if recipients:
            logger.info(
                "Queued alert notification to %d recipients for %s",
                len(recipients),
                rule.name,
            )
    except Exception as exc:
        logger.warning(
            "Failed to queue alert notification for rule %s: %s", rule.id, exc
        )


def _get_alert_recipients(db: Session, rule: AlertRule) -> list[str]:
    """Resolve fallback alert recipients from active admin emails only."""
    try:
        from app.models.system_user import SystemUser

        admins = list(
            db.scalars(
                select(SystemUser.email)
                .where(
                    SystemUser.is_active.is_(True),
                    SystemUser.email.isnot(None),
                )
                .limit(10)
            ).all()
        )
        return [email for email in admins if email]
    except Exception as exc:
        logger.warning("Failed to resolve alert recipients: %s", exc)
        return []


def _evaluate_rule(db: Session, rule: AlertRule) -> tuple[int, int]:
    """Evaluate a single alert rule. Returns (created, resolved)."""
    created = 0
    resolved = 0

    # Build query for latest metric matching rule scope
    stmt = (
        select(DeviceMetric)
        .where(DeviceMetric.metric_type == rule.metric_type)
        .order_by(DeviceMetric.recorded_at.desc())
        .limit(1)
    )
    if rule.device_id:
        stmt = stmt.where(DeviceMetric.device_id == rule.device_id)
    if rule.interface_id:
        stmt = stmt.where(DeviceMetric.interface_id == rule.interface_id)

    metric = db.scalars(stmt).first()
    if not metric:
        return 0, 0

    # Check if metric is stale (older than 10 minutes = skip)
    if metric.recorded_at < datetime.now(UTC) - timedelta(minutes=10):
        return 0, 0

    # Evaluate threshold condition
    op_fn = _OPERATORS.get(rule.operator)
    if not op_fn:
        logger.warning("Unknown operator %s for alert rule %s", rule.operator, rule.id)
        return 0, 0

    breached = op_fn(metric.value, rule.threshold)

    # Find existing open/acknowledged alert for this rule+device+interface
    existing_alert = db.scalars(
        select(Alert).where(
            Alert.rule_id == rule.id,
            Alert.status.in_([AlertStatus.open, AlertStatus.acknowledged]),
            Alert.device_id == metric.device_id,
        )
    ).first()

    if breached:
        # Check duration requirement
        if rule.duration_seconds:
            # Need to verify condition was breached for the full duration
            duration_start = datetime.now(UTC) - timedelta(
                seconds=rule.duration_seconds
            )
            oldest_breach = db.scalars(
                select(DeviceMetric)
                .where(
                    DeviceMetric.device_id == metric.device_id,
                    DeviceMetric.metric_type == rule.metric_type,
                    DeviceMetric.recorded_at >= duration_start,
                )
                .order_by(DeviceMetric.recorded_at.asc())
                .limit(1)
            ).first()
            if not oldest_breach or not op_fn(oldest_breach.value, rule.threshold):
                # Condition hasn't persisted long enough
                return 0, 0

        if not existing_alert:
            # Create new alert
            alert = Alert(
                rule_id=rule.id,
                device_id=metric.device_id,
                interface_id=metric.interface_id,
                metric_type=rule.metric_type,
                measured_value=metric.value,
                status=AlertStatus.open,
                severity=rule.severity,
                triggered_at=datetime.now(UTC),
            )
            db.add(alert)
            db.flush()
            # Record event
            event = AlertEvent(
                alert_id=alert.id,
                status=AlertStatus.open,
                message=f"{rule.name}: {metric.value:.2f} {rule.operator.value} {rule.threshold:.2f}",
            )
            db.add(event)
            created = 1
            logger.info(
                "Alert created: rule=%s device=%s value=%.2f threshold=%.2f",
                rule.name,
                metric.device_id,
                metric.value,
                rule.threshold,
            )
            # Dispatch alert notification
            _notify_alert(db, alert, rule, metric, action="triggered")
    else:
        # Condition no longer breached — auto-resolve
        if existing_alert:
            existing_alert.status = AlertStatus.resolved
            existing_alert.resolved_at = datetime.now(UTC)
            event = AlertEvent(
                alert_id=existing_alert.id,
                status=AlertStatus.resolved,
                message=f"Auto-resolved: {metric.value:.2f} no longer {rule.operator.value} {rule.threshold:.2f}",
            )
            db.add(event)
            resolved = 1
            logger.info(
                "Alert auto-resolved: rule=%s device=%s value=%.2f",
                rule.name,
                metric.device_id,
                metric.value,
            )
            _notify_alert(db, existing_alert, rule, metric, action="resolved")

    return created, resolved
