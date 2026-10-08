from datetime import UTC, datetime, timedelta

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import admin_alerts


def _admin_user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="System",
        last_name="Admin",
        email="admin-alerts@example.com",
    )
    role = Role(name="admin", is_active=True)
    db_session.add_all([user, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()
    return user


def _finding(
    *,
    severity: AlertSeverity = AlertSeverity.warning,
) -> admin_alerts.AlertFinding:
    return admin_alerts.AlertFinding(
        fingerprint="infrastructure:celery:no-workers",
        category="infrastructure",
        source="celery",
        severity=severity,
        title="Celery workers are not responding",
        summary="No Celery workers responded.",
        details={"error": "No workers responding"},
        target_url="/admin/system/health",
    )


def test_sync_alert_opens_once_and_notifies_admin(db_session):
    _admin_user(db_session)

    result = admin_alerts.sync_alert(db_session, _finding())
    db_session.commit()

    assert result == "opened"
    assert db_session.query(AdminAlert).count() == 1
    assert db_session.query(AdminNotification).count() == 1

    result = admin_alerts.sync_alert(db_session, _finding())
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert result == "updated"
    assert alert.status == AlertStatus.open
    assert db_session.query(AdminNotification).count() == 1


def test_sync_alert_escalation_resets_unread_notification(db_session):
    _admin_user(db_session)
    admin_alerts.sync_alert(db_session, _finding())
    db_session.commit()
    notification = db_session.query(AdminNotification).one()
    notification.read_at = datetime.now(UTC)
    db_session.commit()

    result = admin_alerts.sync_alert(
        db_session,
        _finding(severity=AlertSeverity.critical),
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    notification = db_session.query(AdminNotification).one()
    assert result == "escalated"
    assert alert.severity == AlertSeverity.critical
    assert notification.read_at is None


def test_resolve_missing_alerts_resolves_absent_fingerprints(db_session):
    _admin_user(db_session)
    admin_alerts.sync_alert(db_session, _finding())
    db_session.commit()

    resolved = admin_alerts.resolve_missing_alerts(
        db_session,
        managed_prefix="infrastructure:",
        active_fingerprints=set(),
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert resolved == 1
    assert alert.status == AlertStatus.resolved
    assert alert.resolved_at is not None


def test_json_safe_coerces_non_serializable_values():
    from decimal import Decimal
    from uuid import UUID

    out = admin_alerts._json_safe(
        {
            "last_success": datetime(2026, 6, 30, 4, 19, tzinfo=UTC),
            "amount": Decimal("12.50"),
            "id": UUID("00000000-0000-0000-0000-000000000001"),
            "nested": [{"when": datetime(2026, 1, 1, tzinfo=UTC)}],
            "ok": "str",
            "flag": True,
            "none": None,
        }
    )
    import json

    # Must be json.dumps-able (this is exactly what the JSON column does on flush).
    json.dumps(out)
    assert out["last_success"] == "2026-06-30T04:19:00+00:00"
    assert out["amount"] == 12.5
    assert out["id"] == "00000000-0000-0000-0000-000000000001"
    assert out["nested"][0]["when"] == "2026-01-01T00:00:00+00:00"
    assert out["flag"] is True and out["none"] is None


def _finding_with_datetime(*, severity: AlertSeverity) -> admin_alerts.AlertFinding:
    # Mirrors the stale-scheduled-task finding (details=dict(task)) that carries
    # a raw datetime "last_success" and crashed the evaluator on flush.
    return admin_alerts.AlertFinding(
        fingerprint="infrastructure:scheduled-task:router-config-backup",
        category="infrastructure",
        source="scheduled-task",
        severity=severity,
        title="Scheduled task stale: router_config_backup",
        summary="Last success was 2 days ago.",
        details={"name": "router_config_backup", "last_success": datetime.now(UTC)},
    )


def test_sync_alert_with_datetime_details_does_not_crash_on_flush(db_session):
    _admin_user(db_session)

    # Create path.
    assert (
        admin_alerts.sync_alert(
            db_session, _finding_with_datetime(severity=AlertSeverity.warning)
        )
        == "opened"
    )
    db_session.commit()

    # Escalation path — this is the UPDATE flush that previously raised
    # "Object of type datetime is not JSON serializable".
    assert (
        admin_alerts.sync_alert(
            db_session, _finding_with_datetime(severity=AlertSeverity.critical)
        )
        == "escalated"
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert isinstance(alert.details["last_success"], str)


def test_alert_categories_include_cross_app_drift(db_session):
    ctx = admin_alerts.alerts_context(
        db_session,
        category=None,
        status="open",
        severity=None,
        source=None,
    )

    assert "cross_app_drift" in ctx["categories"]


def _sla_finding(
    sla_due_at: datetime,
    *,
    fingerprint: str = "prepaid-coverage:quarantine:acct-1",
    reason_codes: tuple[str, ...] = (),
) -> admin_alerts.AlertFinding:
    return admin_alerts.AlertFinding(
        fingerprint=fingerprint,
        category="prepaid_enforcement",
        source="prepaid_coverage_repair",
        severity=AlertSeverity.warning,
        title="Prepaid coverage evidence needs finance review",
        summary="Contradictory evidence blocks enforcement.",
        details={
            "owner": "finance-billing",
            "reason_codes": list(reason_codes),
            "sla_due_at": sla_due_at.isoformat(),
        },
    )


def test_sync_alert_keeps_sla_deadline_of_still_open_item(db_session):
    _admin_user(db_session)
    first_due = datetime(2026, 7, 31, 4, 1, tzinfo=UTC)

    assert admin_alerts.sync_alert(db_session, _sla_finding(first_due)) == "opened"
    db_session.commit()
    # A later scheduled run recomputes now + SLA; the still-open item keeps its
    # original deadline while the rest of the details still refresh.
    later = _sla_finding(
        first_due + timedelta(days=60), reason_codes=("conflicting_amounts",)
    )
    assert admin_alerts.sync_alert(db_session, later) == "updated"
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert alert.details["sla_due_at"] == first_due.isoformat()
    assert alert.details["reason_codes"] == ["conflicting_amounts"]


def test_sync_alert_reopened_item_gets_fresh_sla_deadline(db_session):
    _admin_user(db_session)
    first_due = datetime(2026, 7, 31, 4, 1, tzinfo=UTC)
    admin_alerts.sync_alert(db_session, _sla_finding(first_due))
    db_session.commit()
    admin_alerts.resolve_missing_alerts(
        db_session,
        managed_prefix="prepaid-coverage:quarantine:",
        active_fingerprints=set(),
    )
    db_session.commit()

    fresh_due = first_due + timedelta(days=60)
    assert admin_alerts.sync_alert(db_session, _sla_finding(fresh_due)) == "opened"
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert alert.status == AlertStatus.open
    assert alert.details["sla_due_at"] == fresh_due.isoformat()


def test_count_open_alerts_counts_only_open_items_past_their_deadline(db_session):
    _admin_user(db_session)
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    prefix = "prepaid-coverage:quarantine:"
    for suffix, due in (
        ("overdue", now - timedelta(hours=1)),
        ("not-yet-due", now + timedelta(hours=1)),
        ("resolved-overdue", now - timedelta(days=2)),
    ):
        admin_alerts.sync_alert(
            db_session, _sla_finding(due, fingerprint=f"{prefix}{suffix}")
        )
    # Other owners' prefixes are never counted.
    admin_alerts.sync_alert(
        db_session,
        _sla_finding(now - timedelta(days=2), fingerprint="other:prefix:overdue"),
    )
    db_session.commit()
    admin_alerts.resolve_missing_alerts(
        db_session,
        managed_prefix=prefix,
        active_fingerprints={f"{prefix}overdue", f"{prefix}not-yet-due"},
    )
    db_session.commit()

    assert admin_alerts.count_open_alerts(db_session, managed_prefix=prefix) == 2
    assert (
        admin_alerts.count_open_alerts(
            db_session, managed_prefix=prefix, overdue_at=now
        )
        == 1
    )
