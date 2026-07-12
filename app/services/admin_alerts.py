"""Admin-facing operational alert lifecycle and inbox helpers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.domain_settings import SettingDomain
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.rbac import (
    Permission,
    Role,
    RolePermission,
    SystemUserPermission,
    SystemUserRole,
)
from app.models.system_user import SystemUser
from app.services import settings_spec

logger = logging.getLogger(__name__)

INFRASTRUCTURE_ALERT_PREFIX = "infrastructure:"
_SEVERITY_RANK = {
    AlertSeverity.info: 0,
    AlertSeverity.warning: 1,
    AlertSeverity.critical: 2,
}
_ADMIN_NOTIFICATION_PERMISSION_KEYS = {
    "*",
    "system:*",
    "system:read",
    "system:settings:read",
}


@dataclass(frozen=True)
class AlertFinding:
    fingerprint: str
    category: str
    source: str
    severity: AlertSeverity
    title: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    target_url: str = "/admin/system/health"


def run_infrastructure_alert_evaluation(db: Session) -> dict[str, int]:
    """Evaluate operational health and sync admin alert state."""
    findings = _collect_infrastructure_findings(db)
    active_fingerprints = {finding.fingerprint for finding in findings}
    opened = escalated = updated = 0

    for finding in findings:
        result = sync_alert(db, finding)
        if result == "opened":
            opened += 1
        elif result == "escalated":
            escalated += 1
        else:
            updated += 1

    resolved = resolve_missing_alerts(
        db,
        managed_prefix=INFRASTRUCTURE_ALERT_PREFIX,
        active_fingerprints=active_fingerprints,
    )
    db.commit()
    return {
        "findings": len(findings),
        "opened": opened,
        "escalated": escalated,
        "updated": updated,
        "resolved": resolved,
    }


def _json_safe(value: Any) -> Any:
    """Coerce a value into something ``json.dumps`` can serialize.

    ``AdminAlert.details`` is a plain JSON column, but finding builders often
    drop raw datetimes into it (e.g. a scheduled task's ``last_success`` from
    ``_build_task_activity``), plus the occasional Decimal/UUID/Enum. Without
    this the entire alert-evaluation task fails on flush with
    "Object of type datetime is not JSON serializable" — every infrastructure
    alert silently stops syncing. Sanitizing here, at the single sink where
    details is persisted, covers all current and future finding sources.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def sync_alert(db: Session, finding: AlertFinding) -> str:
    now = datetime.now(UTC)
    alert = (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == finding.fingerprint)
        .one_or_none()
    )
    if alert is None:
        alert = AdminAlert(
            category=finding.category,
            source=finding.source,
            fingerprint=finding.fingerprint,
            severity=finding.severity,
            status=AlertStatus.open,
            title=finding.title,
            summary=finding.summary,
            details=_json_safe(finding.details),
            target_url=finding.target_url,
            first_seen_at=now,
            last_seen_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(alert)
        db.flush()
        _queue_admin_notifications(db, alert)
        return "opened"

    was_resolved = alert.status == AlertStatus.resolved
    severity_escalated = (
        _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[alert.severity]
    )
    alert.category = finding.category
    alert.source = finding.source
    alert.severity = finding.severity
    alert.title = finding.title
    alert.summary = finding.summary
    alert.details = _json_safe(finding.details)
    alert.target_url = finding.target_url
    alert.last_seen_at = now
    alert.updated_at = now
    if was_resolved:
        alert.status = AlertStatus.open
        alert.resolved_at = None
        alert.acknowledged_at = None
    if was_resolved or severity_escalated:
        db.flush()
        _queue_admin_notifications(db, alert)
        return "opened" if was_resolved else "escalated"
    return "updated"


def resolve_missing_alerts(
    db: Session,
    *,
    managed_prefix: str,
    active_fingerprints: set[str],
) -> int:
    now = datetime.now(UTC)
    alerts = (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint.like(f"{managed_prefix}%"))
        .filter(AdminAlert.status != AlertStatus.resolved)
        .all()
    )
    resolved = 0
    for alert in alerts:
        if alert.fingerprint in active_fingerprints:
            continue
        alert.status = AlertStatus.resolved
        alert.resolved_at = now
        alert.updated_at = now
        resolved += 1
    return resolved


def alerts_context(
    db: Session,
    *,
    category: str | None,
    status: str | None,
    severity: str | None,
    source: str | None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    query = db.query(AdminAlert)
    if category:
        query = query.filter(AdminAlert.category == category)
    if status:
        query = query.filter(AdminAlert.status == AlertStatus(status))
    if severity:
        query = query.filter(AdminAlert.severity == AlertSeverity(severity))
    if source:
        query = query.filter(AdminAlert.source == source)

    total = query.count()
    alerts = (
        query.order_by(
            AdminAlert.status.asc(),
            AdminAlert.severity.desc(),
            AdminAlert.last_seen_at.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    counts = _alert_counts(db)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "alerts": alerts,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_previous_page": page > 1,
        "has_next_page": page < total_pages,
        "category": category or "",
        "status": status or "",
        "severity": severity or "",
        "source": source or "",
        "counts": counts,
        "categories": [
            "infrastructure",
            "network",
            "application",
            "billing",
            "cross_app_drift",
        ],
        "statuses": list(AlertStatus),
        "severities": list(AlertSeverity),
    }


def _int_setting(db: Session, key: str, default: int) -> int:
    raw = settings_spec.resolve_value(db, SettingDomain.network_monitoring, key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def dashboard_alert_summary(db: Session) -> dict[str, object]:
    open_statuses = (AlertStatus.open, AlertStatus.acknowledged)
    rows = (
        db.query(AdminAlert.category, AdminAlert.severity, func.count(AdminAlert.id))
        .filter(AdminAlert.status.in_(open_statuses))
        .group_by(AdminAlert.category, AdminAlert.severity)
        .all()
    )
    by_category: dict[str, dict[str, int]] = {}
    for category, severity, count in rows:
        bucket = by_category.setdefault(
            str(category),
            {"critical": 0, "warning": 0, "info": 0, "total": 0},
        )
        bucket[severity.value] = int(count)
        bucket["total"] += int(count)
    recent = (
        db.query(AdminAlert)
        .filter(AdminAlert.status.in_(open_statuses))
        .order_by(AdminAlert.severity.desc(), AdminAlert.last_seen_at.desc())
        .limit(6)
        .all()
    )
    return {"by_category": by_category, "recent": recent}


def notification_menu_context(
    db: Session,
    *,
    system_user_id: str | None,
    limit: int = 10,
) -> dict[str, object]:
    notifications: list[AdminNotification] = []
    if system_user_id:
        notifications = (
            db.query(AdminNotification)
            .filter(AdminNotification.system_user_id == system_user_id)
            .order_by(AdminNotification.created_at.desc())
            .limit(limit)
            .all()
        )
    unread_count = sum(1 for item in notifications if item.read_at is None)
    return {
        "admin_notifications": notifications,
        "admin_unread_count": unread_count,
    }


def mark_notification_read(
    db: Session, notification_id: str
) -> AdminNotification | None:
    notification = db.get(AdminNotification, notification_id)
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(notification)
    return notification


def acknowledge_alert(db: Session, alert_id: str) -> AdminAlert | None:
    alert = db.get(AdminAlert, alert_id)
    if alert is None:
        return None
    now = datetime.now(UTC)
    alert.status = AlertStatus.acknowledged
    alert.acknowledged_at = now
    alert.updated_at = now
    db.commit()
    return alert


def resolve_alert(db: Session, alert_id: str) -> AdminAlert | None:
    alert = db.get(AdminAlert, alert_id)
    if alert is None:
        return None
    now = datetime.now(UTC)
    alert.status = AlertStatus.resolved
    alert.resolved_at = now
    alert.updated_at = now
    db.commit()
    return alert


def count_unread_admin_notifications(db: Session) -> int:
    return (
        db.query(func.count(AdminNotification.id))
        .filter(AdminNotification.read_at.is_(None))
        .scalar()
        or 0
    )


def _collect_infrastructure_findings(db: Session) -> list[AlertFinding]:
    from app.services import infrastructure_health, web_system_health

    findings: list[AlertFinding] = []
    long_running_minutes = _int_setting(
        db,
        "celery_long_running_task_minutes",
        30,
    )
    reserved_backlog_threshold = _int_setting(
        db,
        "celery_reserved_backlog_threshold",
        100,
    )
    queue_backlog_threshold = _int_setting(
        db,
        "celery_queue_backlog_threshold",
        500,
    )
    try:
        for service in infrastructure_health.check_all_services(db):
            service_name = str(service.name or "service")
            status = str(service.status or "unknown")
            if status == "up":
                continue
            if service_name.lower() == "celery":
                findings.extend(
                    _celery_findings(
                        service,
                        long_running_minutes=long_running_minutes,
                        reserved_backlog_threshold=reserved_backlog_threshold,
                        queue_backlog_threshold=queue_backlog_threshold,
                    )
                )
                continue
            severity = (
                AlertSeverity.critical if status == "down" else AlertSeverity.warning
            )
            findings.append(
                AlertFinding(
                    fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}service:{_slug(service_name)}",
                    category="infrastructure",
                    source="service-health",
                    severity=severity,
                    title=f"{service_name} is {status}",
                    summary=_service_summary(service_name, status, service.details),
                    details={
                        "service": service_name,
                        "status": status,
                        **service.details,
                    },
                )
            )
    except Exception as exc:
        logger.exception("Failed to collect infrastructure service alerts")
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}service-health:evaluator",
                category="infrastructure",
                source="service-health",
                severity=AlertSeverity.critical,
                title="Infrastructure health evaluator failed",
                summary=str(exc)[:255],
                details={"error": str(exc)[:500]},
            )
        )

    replication = web_system_health._build_replication_health(db)
    if replication.get("status") == "degraded":
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}postgres:replication",
                category="infrastructure",
                source="postgres-replication",
                severity=_replication_severity(replication),
                title="PostgreSQL standby needs attention",
                summary=str(replication.get("summary") or "Replication degraded."),
                details={
                    "standbys": replication.get("standbys") or [],
                    "slots": replication.get("slots") or [],
                },
            )
        )
    elif replication.get("status") == "unknown" and replication.get("error"):
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}postgres:replication-query",
                category="infrastructure",
                source="postgres-replication",
                severity=AlertSeverity.warning,
                title="PostgreSQL replication status unavailable",
                summary=str(
                    replication.get("summary") or "Replication status unavailable."
                ),
                details={"error": replication.get("error")},
            )
        )

    for task in web_system_health._build_task_activity(db, limit=200):
        if not task.get("stale"):
            continue
        task_name = str(task.get("task_name") or task.get("name") or "task")
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}scheduled-task:{_slug(task_name)}",
                category="infrastructure",
                source="scheduled-task",
                severity=AlertSeverity.warning,
                title=f"Scheduled task stale: {task.get('name') or task_name}",
                summary=f"Last success was {task.get('age_display') or 'not recorded'}.",
                details=dict(task),
            )
        )
    try:
        findings.extend(_poll_health_findings(db))
    except Exception:
        logger.exception("Infrastructure poll health findings failed")
    try:
        findings.extend(_stale_autodetect_incident_findings(db))
    except Exception:
        logger.exception("Stale auto-detect incident findings failed")
    return findings


def _stale_autodetect_incident_findings(db: Session) -> list[AlertFinding]:
    """Guardrail: auto-detected incidents left open beyond the staleness bar.

    The legacy auto-detect path opens operator-style incidents nothing
    auto-resolves; once stale they keep suppressing billing notices and
    inflating customer-impact counts (they once covered 97% of the fleet), so
    lingering rows are an operational alarm, not background noise.
    """
    from datetime import timedelta

    from app.models.network_monitoring import OutageIncident
    from app.services.topology.outage import AUTO_DETECT_ACTOR, STALE_OPEN_HOURS

    cutoff = datetime.now(UTC) - timedelta(hours=STALE_OPEN_HOURS)
    stale = (
        db.query(func.count(OutageIncident.id), func.min(OutageIncident.started_at))
        .filter(
            OutageIncident.status == "open",
            OutageIncident.declared_by == AUTO_DETECT_ACTOR,
            OutageIncident.started_at < cutoff,
        )
        .one()
    )
    count, oldest = int(stale[0] or 0), stale[1]
    if not count:
        return []
    oldest_display = oldest.date().isoformat() if oldest else "unknown"
    return [
        AlertFinding(
            fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}outage:stale-autodetect",
            category="infrastructure",
            source="outage-hygiene",
            severity=AlertSeverity.critical,
            title="Stale auto-detected outage incidents left open",
            summary=(
                f"{count} system:outage-autodetect incidents have been open "
                f"longer than {STALE_OPEN_HOURS}h (oldest {oldest_display}). "
                "They no longer gate billing suppression (TTL) but must be "
                "triaged/resolved — the auto-detect path never closes them."
            ),
            details={"count": count, "oldest_started_at": oldest},
        )
    ]


def _radius_health_findings(db: Session) -> list[AlertFinding]:
    """Customer-experience alarms from the RADIUS health task's heartbeat.

    Reads the last completed run's counters (cache-only — never touches the
    radius DB from the evaluator). Task staleness itself is covered by the
    generic scheduled-task finding; these are the domain conditions:
    accounting stopped flowing, radacct unreadable, or enforcement drift
    (suspended customers still holding live sessions).
    """
    from app.services.radius_health import HEARTBEAT_TASK
    from app.services.task_heartbeat import snapshot

    beat = snapshot(HEARTBEAT_TASK)
    if beat["last_success_age_seconds"] is None:
        return []  # task has never run (fresh deploy) — staleness finding owns this
    result = beat["result"]
    prefix = f"{INFRASTRUCTURE_ALERT_PREFIX}radius:"
    findings: list[AlertFinding] = []

    freshness_alert_seconds = _int_setting(
        db, "radius_acct_freshness_alert_seconds", 900
    )
    freshness = result.get("acct_freshness_seconds")
    open_sessions = int(result.get("open_sessions") or 0)
    if not result.get("radacct_read_ok"):
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}radacct-unreachable",
                category="infrastructure",
                source="radius-health",
                severity=AlertSeverity.critical,
                title="RADIUS accounting DB unreadable",
                summary=(
                    "The last health pass could not read every configured "
                    "radacct source — session state and usage accounting are "
                    "flying blind."
                ),
                details=_json_safe(result),
            )
        )
    elif "radacct_schema_ok" in result and not result.get("radacct_schema_ok"):
        capacity = int(result.get("radacct_nasportid_capacity") or 0)
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}radacct-schema-incompatible",
                category="infrastructure",
                source="radius-health",
                severity=AlertSeverity.critical,
                title="RADIUS accounting schema rejects valid sessions",
                summary=(
                    "radacct.nasportid accepts only "
                    f"{capacity} characters; 253 are required for RADIUS "
                    "NAS-Port-Id values. Longer vendor interface identifiers "
                    "cause complete accounting rows to be dropped."
                ),
                details=_json_safe(result),
            )
        )
    elif open_sessions and (
        freshness is None or float(freshness) > freshness_alert_seconds
    ):
        display = "no update seen" if freshness is None else f"{int(freshness)}s ago"
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}acct-stale",
                category="infrastructure",
                source="radius-health",
                severity=AlertSeverity.critical,
                title="RADIUS accounting has stopped flowing",
                summary=(
                    f"Newest interim update across {open_sessions} open "
                    f"sessions: {display} (threshold "
                    f"{freshness_alert_seconds}s). NAS accounting or the "
                    "radius DB path is broken."
                ),
                details=_json_safe(result),
            )
        )

    if int(result.get("probe_configured") or 0) and not int(
        result.get("probe_responded") or 0
    ):
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}auth-probe-failed",
                category="infrastructure",
                source="radius-health",
                severity=AlertSeverity.critical,
                title="RADIUS auth probe not answering",
                summary=(
                    "The synthetic Access-Request probe got no response from "
                    "FreeRADIUS (timeouts on every attempt) — new customer auth "
                    "attempts are likely failing even if accounting still looks "
                    "fresh."
                ),
                details=_json_safe(result),
            )
        )

    suspended_active = int(result.get("suspended_with_session") or 0)
    if suspended_active:
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}enforcement-drift",
                category="infrastructure",
                source="radius-health",
                severity=AlertSeverity.warning,
                title="Suspended customers still hold live sessions",
                summary=(
                    f"{suspended_active} suspended/blocked subscriptions have "
                    "an active RADIUS session — enforcement is not landing on "
                    "the NAS (CoA/disconnect path)."
                ),
                details=_json_safe(result),
            )
        )

    return findings


def _poll_health_findings(db: Session) -> list[AlertFinding]:
    """Dead-man switch for the native infrastructure poller.

    With Zabbix retired this poller is the thing that notices device silence,
    so its own silence must be noticed here: a stalled run, a stuck
    single-flight lock, failing VictoriaMetrics writes, or ping data going
    stale all raise an admin alert (and auto-resolve when healthy again).
    """
    from app.services.infrastructure_polling import poll_health_snapshot

    snapshot = poll_health_snapshot(db)
    interval = int(snapshot["poll_interval_seconds"])
    stale_run_after = interval * 3
    skip_streak_threshold = _int_setting(
        db, "infrastructure_poll_skip_streak_threshold", 5
    )
    stale_ping_minutes = _int_setting(db, "infrastructure_poll_stale_ping_minutes", 10)
    prefix = f"{INFRASTRUCTURE_ALERT_PREFIX}poll:"
    findings: list[AlertFinding] = []

    age = snapshot["last_success_age_seconds"]
    if age is None or age > stale_run_after:
        display = "never" if age is None else f"{int(age)}s ago"
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}stalled",
                category="infrastructure",
                source="infrastructure-poll",
                severity=AlertSeverity.critical,
                title="Native infrastructure poll stalled",
                summary=(
                    f"Last successful poll run: {display} "
                    f"(threshold {stale_run_after}s = 3x beat interval)."
                ),
                details=_json_safe(snapshot),
            )
        )

    if snapshot["skip_streak"] >= skip_streak_threshold:
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}lock-stuck",
                category="infrastructure",
                source="infrastructure-poll",
                severity=AlertSeverity.critical,
                title="Infrastructure poll runs repeatedly skipped",
                summary=(
                    f"{snapshot['skip_streak']} consecutive runs skipped with "
                    "already_running — the single-flight advisory lock looks "
                    "stuck (see pg_locks for objid 7235920)."
                ),
                details=_json_safe(snapshot),
            )
        )

    if snapshot["interface_write_failed"]:
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}vm-write-failed",
                category="infrastructure",
                source="infrastructure-poll",
                severity=AlertSeverity.warning,
                title="Interface counter push to VictoriaMetrics failing",
                summary=(
                    f"Last poll run failed to write "
                    f"{snapshot['interface_write_failed']} counter lines — "
                    "admin interface bandwidth graphs will go blank."
                ),
                details=_json_safe(snapshot),
            )
        )

    ping_age = snapshot["newest_ping_age_seconds"]
    if snapshot["pingable_devices"] and (
        ping_age is None or ping_age > stale_ping_minutes * 60
    ):
        display = "never" if ping_age is None else f"{int(ping_age)}s ago"
        findings.append(
            AlertFinding(
                fingerprint=f"{prefix}ping-stale",
                category="infrastructure",
                source="infrastructure-poll",
                severity=AlertSeverity.critical,
                title="No fresh device ping results",
                summary=(
                    f"Newest last_ping_at across "
                    f"{snapshot['pingable_devices']} pingable devices: "
                    f"{display} (threshold {stale_ping_minutes}m) — outage "
                    "detection is flying blind."
                ),
                details=_json_safe(snapshot),
            )
        )

    return findings


def _celery_findings(
    service: object,
    *,
    long_running_minutes: int,
    reserved_backlog_threshold: int,
    queue_backlog_threshold: int,
) -> list[AlertFinding]:
    details = getattr(service, "details", {}) or {}
    status = str(getattr(service, "status", "unknown") or "unknown")
    if status == "down":
        return [
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}celery:no-workers",
                category="infrastructure",
                source="celery",
                severity=AlertSeverity.critical,
                title="Celery workers are not responding",
                summary=str(details.get("error") or "No Celery workers responded."),
                details=dict(details),
            )
        ]
    findings: list[AlertFinding] = []
    long_running = list(details.get("long_running_tasks_over_30m") or [])
    if long_running:
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}celery:long-running-tasks",
                category="infrastructure",
                source="celery",
                severity=AlertSeverity.warning,
                title="Celery has long-running tasks",
                summary=f"{len(long_running)} task(s) have run for over {long_running_minutes} minutes.",
                details={"tasks": long_running[:20]},
            )
        )
    reserved_count = int(details.get("reserved_tasks") or 0)
    if reserved_count > reserved_backlog_threshold:
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}celery:reserved-backlog",
                category="infrastructure",
                source="celery",
                severity=AlertSeverity.warning,
                title="Celery reserved task backlog is high",
                summary=f"{reserved_count} reserved tasks are waiting.",
                details={
                    "reserved_tasks": reserved_count,
                    "threshold": reserved_backlog_threshold,
                },
            )
        )
    queue_lengths = dict(details.get("queue_lengths") or {})
    for queue_name, length in queue_lengths.items():
        queue_length = int(length or 0)
        if queue_length <= queue_backlog_threshold:
            continue
        findings.append(
            AlertFinding(
                fingerprint=f"{INFRASTRUCTURE_ALERT_PREFIX}celery:queue:{_slug(str(queue_name))}",
                category="infrastructure",
                source="celery",
                severity=AlertSeverity.warning,
                title=f"Celery queue backlog: {queue_name}",
                summary=f"{queue_length} task(s) are waiting in {queue_name}.",
                details={
                    "queue": queue_name,
                    "length": queue_length,
                    "threshold": queue_backlog_threshold,
                },
            )
        )
    return findings


def _replication_severity(replication: dict[str, object]) -> AlertSeverity:
    standbys_raw = replication.get("standbys")
    slots_raw = replication.get("slots")
    standbys = standbys_raw if isinstance(standbys_raw, list) else []
    slots = slots_raw if isinstance(slots_raw, list) else []
    if slots and not standbys:
        return AlertSeverity.critical
    return AlertSeverity.warning


def _queue_admin_notifications(db: Session, alert: AdminAlert) -> int:
    targets = _target_admin_users(db)
    target_url = alert.target_url or f"/admin/alerts?category={alert.category}"
    created_or_reset = 0
    for user in targets:
        notification = (
            db.query(AdminNotification)
            .filter(AdminNotification.alert_id == alert.id)
            .filter(AdminNotification.system_user_id == user.id)
            .one_or_none()
        )
        if notification is None:
            db.add(
                AdminNotification(
                    alert_id=alert.id,
                    system_user_id=user.id,
                    title=alert.title,
                    body=alert.summary,
                    target_url=target_url,
                )
            )
        else:
            notification.title = alert.title
            notification.body = alert.summary
            notification.target_url = target_url
            notification.read_at = None
        created_or_reset += 1
    return created_or_reset


def _target_admin_users(db: Session) -> list[SystemUser]:
    role_targets = (
        db.query(SystemUser.id)
        .join(SystemUserRole, SystemUserRole.system_user_id == SystemUser.id)
        .join(Role, Role.id == SystemUserRole.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .filter(SystemUser.is_active.is_(True))
        .filter(Role.is_active.is_(True))
        .filter(
            or_(
                Role.name == "admin",
                Permission.key.in_(_ADMIN_NOTIFICATION_PERMISSION_KEYS),
            )
        )
    )
    direct_targets = (
        db.query(SystemUser.id)
        .join(
            SystemUserPermission,
            SystemUserPermission.system_user_id == SystemUser.id,
        )
        .join(Permission, Permission.id == SystemUserPermission.permission_id)
        .filter(SystemUser.is_active.is_(True))
        .filter(Permission.key.in_(_ADMIN_NOTIFICATION_PERMISSION_KEYS))
    )
    target_ids = {row[0] for row in role_targets.union(direct_targets).all()}
    if not target_ids:
        return []
    return db.query(SystemUser).filter(SystemUser.id.in_(target_ids)).all()


def _alert_counts(db: Session) -> dict[str, int]:
    rows = (
        db.query(AdminAlert.status, func.count(AdminAlert.id))
        .group_by(AdminAlert.status)
        .all()
    )
    counts = {"open": 0, "acknowledged": 0, "resolved": 0}
    for status, count in rows:
        counts[status.value] = int(count)
    return counts


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def _service_summary(service_name: str, status: str, details: dict[str, Any]) -> str:
    error = details.get("error")
    if error:
        return str(error)[:255]
    return f"{service_name} health check is {status}."
