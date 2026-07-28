"""Access-control health reports outcomes, not only task execution."""

from app.models.network_monitoring import AlertSeverity
from app.services import admin_alerts


def test_access_control_finding_reports_unconverged_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.job_heartbeat.get_last_result",
        lambda _task: {
            "status": "degraded",
            "detail": {
                "accounting_target_configured": 1,
                "radius_projection_unconverged": 4,
                "account_projection_errors": 0,
            },
        },
    )

    findings = admin_alerts._access_control_findings()

    assert len(findings) == 1
    assert findings[0].severity == AlertSeverity.warning
    assert "4 RADIUS projection" in findings[0].summary


def test_access_control_finding_is_critical_without_projection_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.job_heartbeat.get_last_result",
        lambda _task: {
            "status": "degraded",
            "detail": {
                "accounting_target_configured": 0,
                "radius_projection_unconverged": 0,
                "account_projection_errors": 0,
            },
        },
    )

    finding = admin_alerts._access_control_findings()[0]

    assert finding.severity == AlertSeverity.critical


def test_access_control_finding_reports_failed_disconnects(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.job_heartbeat.get_last_result",
        lambda _task: {
            "status": "degraded",
            "detail": {
                "accounting_target_configured": 1,
                "radius_projection_unconverged": 0,
                "radius_unbuildable_logins": 0,
                "account_projection_errors": 0,
                "kick_failed": 3,
                "kicks_capped": 2,
            },
        },
    )

    finding = admin_alerts._access_control_findings()[0]

    assert "3 failed disconnect" in finding.summary
    assert "2 deferred" in finding.summary


def test_access_control_finding_reports_repeated_single_flight_overlap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.services.job_heartbeat.get_last_result",
        lambda _task: {"status": "ok", "detail": {}},
    )
    monkeypatch.setattr(
        "app.services.task_heartbeat.snapshot",
        lambda _task: {
            "last_success_age_seconds": 90,
            "skip_streak": 3,
            "result": {},
        },
    )

    finding = admin_alerts._access_control_findings()[0]

    assert finding.severity == AlertSeverity.warning
    assert "3 consecutive overlapping" in finding.summary


def test_access_control_finding_resolves_at_parity(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.job_heartbeat.get_last_result",
        lambda _task: {"status": "ok", "detail": {}},
    )

    assert admin_alerts._access_control_findings() == []
