"""Celery task for evaluating alert rules against device metrics."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.network_monitoring import (
    Alert,
    AlertEvent,
    AlertOperator,
    AlertRule,
    AlertStatus,
    DeviceMetric,
)

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
    db = SessionLocal()
    rules_checked = 0
    alerts_created = 0
    alerts_resolved = 0

    try:
        rules = list(
            db.scalars(
                select(AlertRule).where(AlertRule.is_active.is_(True))
            ).all()
        )

        for rule in rules:
            rules_checked += 1
            try:
                created, resolved = _evaluate_rule(db, rule)
                alerts_created += created
                alerts_resolved += resolved
            except Exception:
                logger.exception("Error evaluating alert rule %s", rule.id)
                db.rollback()

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Alert rule evaluation failed")
        raise
    finally:
        db.close()

    stats = {
        "rules_checked": rules_checked,
        "alerts_created": alerts_created,
        "alerts_resolved": alerts_resolved,
    }
    logger.info("Alert rule evaluation complete: %s", stats)
    return stats


def _evaluate_rule(db, rule: AlertRule) -> tuple[int, int]:
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
            duration_start = datetime.now(UTC) - timedelta(seconds=rule.duration_seconds)
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

    return created, resolved
