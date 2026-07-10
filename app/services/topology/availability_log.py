"""Availability → uptime-alert bridge (Infrastructure SLA, Phase 0/R1).

The SLA/uptime report (`network_monitoring.uptime_report`) derives downtime
intervals solely from `Alert` rows with `metric_type=uptime`. In production
nothing else creates those (see INFRASTRUCTURE_SLA_PERFORMANCE.md Phase 0).
Without a bridge every uptime % reads 100%.

This module is that bridge. The live-status warmer already detects device
state transitions; on each transition we open an uptime `Alert` when a device
goes ``down`` and resolve it when the device recovers (or we lose visibility).
The intervals it produces are exactly what `uptime_report` merges, so SLA and
the live wallboard share one source of truth (resolving R3 drift).

Gated by ``settings.sla_availability_log_enabled`` (default OFF) at the call
site in the warmer — this module itself is side-effect-only-on-call.

Downtime policy: only ``down`` counts as downtime. ``problem`` (host up, active
trigger) is *degraded*, not an outage, and ``unknown`` (disabled / in
maintenance / no signal) is untrusted — entering either from ``down`` closes
the open interval so we never accumulate downtime we cannot confirm.

Known limitation: resolution only happens via a warmer transition. If the flag
is turned OFF (or a down device leaves the warm set) while an interval is open,
the recovery is never recorded and the interval stays open, accruing downtime to
``period_end`` on every report. ``uptime_report`` only iterates *active*
devices, so the blast radius is bounded, but a future flag-disable path should
resolve dangling auto-intervals. Acceptable now given default-OFF + single
warmer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.network_monitoring import (
    Alert,
    AlertRule,
    AlertSeverity,
    AlertStatus,
    MetricType,
)

DOWN = "down"

# Stable name of the single synthetic system rule every availability alert hangs
# off (Alert.rule_id is NOT NULL). Looked up by name, created once on first use.
_SYSTEM_RULE_NAME = "Device availability (auto)"


def _now() -> datetime:
    return datetime.now(UTC)


def get_uptime_rule(session: Session) -> AlertRule:
    """Get-or-create the synthetic system rule for availability uptime alerts."""
    rule = (
        session.query(AlertRule)
        .filter(
            AlertRule.metric_type == MetricType.uptime,
            AlertRule.name == _SYSTEM_RULE_NAME,
            AlertRule.device_id.is_(None),
        )
        .first()
    )
    if rule is None:
        rule = AlertRule(
            name=_SYSTEM_RULE_NAME,
            metric_type=MetricType.uptime,
            threshold=0.0,
            severity=AlertSeverity.critical,
            device_id=None,
            is_active=True,
            notes="Auto-managed: device availability downtime intervals for SLA.",
        )
        session.add(rule)
        session.flush()
    return rule


def _open_uptime_alert(session: Session, device_id) -> Alert | None:
    return (
        session.query(Alert)
        .filter(
            Alert.device_id == device_id,
            Alert.metric_type == MetricType.uptime,
            Alert.status != AlertStatus.resolved,
        )
        .order_by(Alert.triggered_at.desc())
        .first()
    )


def record_transition(
    session: Session,
    device,
    new_status: str,
    *,
    now: datetime | None = None,
) -> None:
    """Open/resolve the device's uptime interval for a state transition.

    Caller (the warmer) invokes this only when ``device.live_status`` actually
    changed, and only when the SLA bridge flag is on. Idempotent: re-opening an
    already-open interval, or resolving when none is open, is a no-op.
    """
    now = now or _now()
    open_alert = _open_uptime_alert(session, device.id)
    if new_status == DOWN:
        if open_alert is not None:
            return  # already accumulating downtime for this device
        rule = get_uptime_rule(session)
        session.add(
            Alert(
                rule_id=rule.id,
                device_id=device.id,
                metric_type=MetricType.uptime,
                measured_value=0.0,
                status=AlertStatus.open,
                severity=AlertSeverity.critical,
                triggered_at=now,
                notes="Device unreachable (availability warmer).",
            )
        )
    else:
        # Recovered, degraded, or lost-visibility — close any open interval.
        if open_alert is not None:
            open_alert.status = AlertStatus.resolved
            open_alert.resolved_at = now
