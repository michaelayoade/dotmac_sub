from datetime import UTC, datetime

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
