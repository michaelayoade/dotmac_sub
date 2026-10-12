from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.network_monitoring import AlertSeverity, AlertStatus
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import observability
from app.services.router_health import commands, findings
from app.services.router_health.contracts import (
    CheckId,
    HealthFinding,
    HealthLayer,
    HealthPriority,
    SubjectKind,
)
from app.services.router_health.policy import parse_policy
from app.services.router_health.posture import (
    PostureThresholds,
    RouterInput,
    SnapshotInput,
    compute_posture,
)
from tests.services.router_health.support import TODAY, build_export, default_policy

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def make(
    router_id,
    check=CheckId.ntp_client_off,
    *,
    layer=HealthLayer.posture,
    qualifier="0",
    priority=HealthPriority.medium,
):
    return HealthFinding(
        layer=layer,
        subject_kind=SubjectKind.router,
        subject_id=router_id,
        check=check,
        qualifier=qualifier,
        priority=priority,
        title="t",
        impact="impact",
        evidence={"k": 1},
        fix="fix",
        target_url=f"/admin/network/routers/{router_id}",
        router_id=router_id,
    )


def admin(db_session) -> None:
    user = SystemUser(first_name="A", last_name="B", email="rh-admin@example.com")
    role = Role(name="admin", is_active=True)
    db_session.add_all([user, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    db_session.commit()


def test_fingerprint_grammar_and_length() -> None:
    rid = uuid.uuid4()
    for qualifier in ("0", "splynx-blocked", "BAD_QUALIFIER%", "x" * 80):
        fp = findings.fingerprint(make(rid, qualifier=qualifier))
        assert fp.startswith("network:router-health:posture:")
        assert "_" not in fp and "%" not in fp
        assert len(fp) <= 180


def test_priority_to_severity_mapping() -> None:
    assert findings.SEVERITY_BY_PRIORITY == {
        HealthPriority.critical: AlertSeverity.critical,
        HealthPriority.high: AlertSeverity.warning,
        HealthPriority.medium: AlertSeverity.warning,
        HealthPriority.low: AlertSeverity.info,
    }


def test_notify_false_creates_no_notifications_and_true_does(db_session) -> None:
    admin(db_session)
    rid = uuid.uuid4()
    obs = findings.to_observability_finding(make(rid), mode="shadow")
    observability.record_finding(db_session, obs, notify=False)
    db_session.commit()
    assert db_session.query(AdminAlert).count() == 1
    assert db_session.query(AdminNotification).count() == 0
    obs2 = findings.to_observability_finding(
        make(uuid.uuid4(), CheckId.snmp_exposed), mode="alert"
    )
    observability.record_finding(db_session, obs2)
    db_session.commit()
    assert db_session.query(AdminNotification).count() == 1


def test_sync_resolves_only_judged_scopes_and_other_prefixes_survive(
    db_session,
) -> None:
    router_a, router_b = uuid.uuid4(), uuid.uuid4()
    other = AdminAlert(
        category="infrastructure",
        source="x",
        fingerprint="infrastructure:keep",
        severity=AlertSeverity.info,
        status=AlertStatus.open,
        title="keep",
    )
    db_session.add(other)
    db_session.commit()
    both = [make(router_a), make(router_b)]
    scope = lambda rid: findings.Scope(HealthLayer.posture, rid, CheckId.ntp_client_off)  # noqa: E731
    findings.sync_findings(
        db_session,
        findings=both,
        scopes=[scope(router_a), scope(router_b)],
        mode="shadow",
    )
    db_session.commit()
    # Next run: router A cleared and judged; router B not judged (unparsed).
    out = findings.sync_findings(
        db_session, findings=[], scopes=[scope(router_a)], mode="shadow"
    )
    db_session.commit()
    assert out.resolved == 1
    states = {a.fingerprint: a.status for a in db_session.query(AdminAlert).all()}
    assert states[findings.fingerprint(make(router_a))] is AlertStatus.resolved
    assert states[findings.fingerprint(make(router_b))] is AlertStatus.open
    assert states["infrastructure:keep"] is AlertStatus.open


def test_inactive_router_resolves_everything(db_session) -> None:
    rid = uuid.uuid4()
    findings.sync_findings(
        db_session,
        findings=[make(rid), make(rid, CheckId.snmp_exposed)],
        scopes=[],
        mode="shadow",
    )
    db_session.commit()
    resolved = findings.resolve_subjects(
        db_session, layer=HealthLayer.posture, subject_ids=[rid]
    )
    assert resolved == 2


def test_cap_keeps_most_urgent() -> None:
    rid = uuid.uuid4()
    items = [
        make(rid, CheckId.ntp_client_off, priority=HealthPriority.low),
        make(rid, CheckId.snmp_exposed, priority=HealthPriority.critical),
    ]
    kept, dropped = findings.cap_findings(items, 1)
    assert dropped == 1 and kept[0].check is CheckId.snmp_exposed


def test_unparsed_router_keeps_posture_findings_end_to_end(db_session) -> None:
    ok_id, bad_id = uuid.uuid4(), uuid.uuid4()
    good = build_export(ntp=None)
    policy = parse_policy(default_policy().model_dump(mode="json"), today=TODAY)

    def routers(bad_text: str):
        return [
            RouterInput(
                ok_id, "ok", "192.0.2.50", SnapshotInput(uuid.uuid4(), NOW, good)
            ),
            RouterInput(
                bad_id, "bad", "192.0.2.51", SnapshotInput(uuid.uuid4(), NOW, bad_text)
            ),
        ]

    def run(bad_text: str):
        comp = compute_posture(
            routers=routers(bad_text),
            loaded_policy=policy,
            suspended_list="suspended",
            thresholds=PostureThresholds(),
            now=NOW,
            today=TODAY,
        )
        findings.sync_findings(
            db_session, findings=comp.findings, scopes=comp.scopes, mode="shadow"
        )
        db_session.commit()
        return comp

    run(build_export(ntp=None, identity="Bad"))  # both have an ntp finding
    comp = run('/ip firewall filter\nadd comment="broken\n')  # bad becomes unparseable
    open_fps = {
        a.fingerprint
        for a in db_session.query(AdminAlert).filter(
            AdminAlert.status == AlertStatus.open
        )
    }
    ntp_fp = lambda rid: f"network:router-health:posture:{rid}:ntp-client-off:0"  # noqa: E731
    assert ntp_fp(bad_id) in open_fps  # carried forward, not resolved
    assert ntp_fp(ok_id) in open_fps
    assert any(f.check is CheckId.snapshot_unparseable for f in comp.findings)


def test_commands_commit_once_and_stage_event(db_session) -> None:
    rid = uuid.uuid4()
    outcome = commands.reconcile_router_posture(
        db_session,
        command=commands.ProjectFindingsCommand(
            context=commands.system_context("test"),
            findings=(make(rid),),
            scopes=(findings.Scope(HealthLayer.posture, rid, CheckId.ntp_client_off),),
            mode="shadow",
        ),
    )
    assert outcome.opened == 1
    from app.models.event_store import EventStore

    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "router_health.reconciled")
        .count()
        == 1
    )


def test_retire_resolves_all_and_audits(db_session) -> None:
    rid = uuid.uuid4()
    findings.sync_findings(db_session, findings=[make(rid)], scopes=[], mode="shadow")
    db_session.commit()
    resolved = commands.retire_router_health_findings(
        db_session,
        command=commands.RetireFindingsCommand(commands.system_context("rollback")),
    )
    assert resolved == 1
    from app.models.audit import AuditEvent

    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == commands.AUDIT_RETIRED)
        .count()
        == 1
    )


@pytest.mark.parametrize("mode,expected", [("shadow", 0), ("alert", 1)])
def test_mode_controls_notifications_through_sync(db_session, mode, expected) -> None:
    admin(db_session)
    findings.sync_findings(
        db_session, findings=[make(uuid.uuid4())], scopes=[], mode=mode
    )
    db_session.commit()
    assert db_session.query(AdminNotification).count() == expected
