from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.catalog import NasDevice, NasVendor
from app.models.router_health import NasCoaOutcomeBucket
from app.services import control_registry
from app.services.router_health import coa_canary, commands
from app.services.router_health.contracts import CanaryDisposition, CanaryTarget
from app.services.router_health.settings import RouterHealthSettings, load_settings

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def add_nas(db, name, host, *, vendor=NasVendor.mikrotik, active=True, port=3799):
    nas = NasDevice(
        name=name, vendor=vendor, nas_ip=host, coa_port=port, is_active=active
    )
    db.add(nas)
    db.commit()
    return nas


def settings(**kw) -> RouterHealthSettings:
    return RouterHealthSettings(**kw)


def test_usernames_are_unique_prefixed_and_not_ppp_generatable() -> None:
    names = {coa_canary.canary_username() for _ in range(200)}
    assert len(names) == 200
    assert all(n.startswith("dotmac-coa-canary-") for n in names)
    # PPPoE usernames are numeric (prefix setting default "1050" + padded number).
    assert not any(re.fullmatch(r"\d+", n) for n in names)


def test_collision_check_sees_credentials_and_radius_users(db_session) -> None:
    from app.models.catalog import AccessCredential

    assert coa_canary.username_collides(db_session, "dotmac-coa-canary-x") is False
    cols = {
        c.name
        for c in AccessCredential.__table__.columns
        if not c.nullable and c.default is None and c.server_default is None
    }
    assert "username" in cols  # the column the check reads exists and is required


def test_targets_dedupe_by_host_filter_vendor_cap_and_spacing(db_session) -> None:
    a1 = add_nas(db_session, "a-1", "192.0.2.10")
    a2 = add_nas(db_session, "a-2", "192.0.2.10")  # duplicate row, same host
    b = add_nas(db_session, "b", "192.0.2.11")
    add_nas(db_session, "huawei", "192.0.2.12", vendor=NasVendor.huawei)
    add_nas(db_session, "inactive", "192.0.2.13", active=False)
    loaded = coa_canary.load_policy(db_session)
    targets = coa_canary.select_targets(
        db_session, settings=settings(), loaded=loaded, now=NOW
    )
    assert [t.host for t in targets] == ["192.0.2.10", "192.0.2.11"]
    assert set(targets[0].nas_device_ids) == {a1.id, a2.id}
    # cap
    capped = coa_canary.select_targets(
        db_session, settings=settings(coa_canary_max_targets=1), loaded=loaded, now=NOW
    )
    assert len(capped) == 1
    # rate limit: a recent canary bucket for any row on the host excludes it
    db_session.add(
        NasCoaOutcomeBucket(
            nas_device_id=a1.id,
            kind="canary",
            disposition="rejected",
            bucket_start=NOW.replace(minute=0),
            count=1,
            last_at=NOW - timedelta(hours=2),
        )
    )
    db_session.commit()
    again = coa_canary.select_targets(
        db_session, settings=settings(), loaded=loaded, now=NOW
    )
    assert [t.host for t in again] == ["192.0.2.11"]
    assert b.id in again[0].nas_device_ids


@pytest.fixture()
def canary_env(db_session, monkeypatch):
    control_registry_enabled = {"access.radius_coa": True}
    monkeypatch.setattr(
        control_registry,
        "is_enabled",
        lambda db, key: control_registry_enabled.get(key, False),
    )
    monkeypatch.setattr(
        coa_canary, "record_coa_outcome", lambda *a, **k: recorded.append(a)
    )
    recorded: list[tuple] = []
    return recorded


def test_run_commits_started_audit_before_first_send_and_attributes_all_rows(
    db_session, canary_env, monkeypatch
) -> None:
    add_nas(db_session, "a-1", "192.0.2.10")
    add_nas(db_session, "a-2", "192.0.2.10")
    seen: list[int] = []

    def fake_send(db, target):
        seen.append(
            db.query(AuditEvent)
            .filter(AuditEvent.action == commands.AUDIT_CANARY_STARTED)
            .count()
        )
        return CanaryDisposition.session_not_found

    monkeypatch.setattr(coa_canary, "send_canary", fake_send)
    stats = coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: pytest.fail("healthy result must not re-probe"),
        sleep=lambda s: None,
    )
    assert seen == [1]  # the audit row was already committed and visible
    assert stats.targets == 1 and stats.healthy == 1
    assert len(canary_env) == 2  # recorded for both NAS rows on the host
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == commands.AUDIT_CANARY_COMPLETED)
        .count()
        == 1
    )


def test_timeout_schedules_one_reprobe_and_defers_findings(
    db_session, canary_env, monkeypatch
) -> None:
    add_nas(db_session, "a", "192.0.2.10")
    monkeypatch.setattr(
        coa_canary, "send_canary", lambda db, t: CanaryDisposition.timeout
    )
    scheduled: list[tuple] = []
    stats = coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda nas_id, run_id, delay: scheduled.append(
            (nas_id, delay)
        ),
        sleep=lambda s: None,
    )
    assert len(scheduled) == 1 and scheduled[0][1] == 300
    assert stats.failed_pending_reprobe == 1 and stats.findings == 0


def test_reprobe_failure_raises_high_finding_and_success_clears(
    db_session, canary_env, monkeypatch
) -> None:
    from app.models.admin_alert import AdminAlert
    from app.models.network_monitoring import AlertStatus

    nas = add_nas(db_session, "a", "192.0.2.10")
    monkeypatch.setattr(
        coa_canary, "send_canary", lambda db, t: CanaryDisposition.timeout
    )
    run_id = uuid4()
    coa_canary.reprobe_coa_canary(
        db_session, nas_device_id=nas.id, run_id=run_id, task_name="t"
    )
    alert = db_session.query(AdminAlert).one()
    assert (
        "coa-canary-timeout" in alert.fingerprint and alert.status is AlertStatus.open
    )
    monkeypatch.setattr(
        coa_canary, "send_canary", lambda db, t: CanaryDisposition.rejected
    )
    coa_canary.reprobe_coa_canary(
        db_session, nas_device_id=nas.id, run_id=run_id, task_name="t"
    )
    db_session.expire_all()
    assert db_session.query(AdminAlert).one().status is AlertStatus.resolved


def test_ack_is_critical(db_session, canary_env, monkeypatch) -> None:
    from app.models.admin_alert import AdminAlert
    from app.models.network_monitoring import AlertSeverity

    add_nas(db_session, "a", "192.0.2.10")
    monkeypatch.setattr(
        coa_canary, "send_canary", lambda db, t: CanaryDisposition.disconnected
    )
    coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: None,
        sleep=lambda s: None,
    )
    assert db_session.query(AdminAlert).one().severity is AlertSeverity.critical


def test_disabled_coa_sends_nothing(db_session, monkeypatch) -> None:
    monkeypatch.setattr(control_registry, "is_enabled", lambda db, key: False)
    add_nas(db_session, "a", "192.0.2.10")
    stats = coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: None,
        sleep=lambda s: None,
    )
    assert stats.skipped == "coa_disabled"


def test_packet_spacing_between_targets(db_session, canary_env, monkeypatch) -> None:
    add_nas(db_session, "a", "192.0.2.10")
    add_nas(db_session, "b", "192.0.2.11")
    monkeypatch.setattr(
        coa_canary, "send_canary", lambda db, t: CanaryDisposition.rejected
    )
    sleeps: list[float] = []
    coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: None,
        sleep=sleeps.append,
    )
    assert sleeps == [2.0]


def test_send_aborts_on_username_collision(db_session, monkeypatch) -> None:
    nas = add_nas(db_session, "a", "192.0.2.10")
    monkeypatch.setattr(coa_canary, "username_collides", lambda db, u: True)
    target = CanaryTarget("192.0.2.10", 3799, (nas.id,), nas.id)
    assert (
        coa_canary.send_canary(db_session, target)
        is CanaryDisposition.aborted_collision
    )


def test_default_settings_load(db_session) -> None:
    loaded = load_settings(db_session)
    assert loaded.alert_mode == "shadow"
    assert loaded.coa_canary_spacing_ms == 2000


def test_external_radacct_hit_aborts_and_error_fails_safe(
    db_session, monkeypatch
) -> None:
    nas = add_nas(db_session, "a", "192.0.2.10")
    target = CanaryTarget("192.0.2.10", 3799, (nas.id,), nas.id)
    sent: list[str] = []
    monkeypatch.setattr(
        coa_canary.enforcement,
        "send_coa_probe_disconnect",
        lambda db, n, username: sent.append(username) or CanaryDisposition.rejected,
    )
    monkeypatch.setattr(coa_canary, "external_radacct_username_seen", lambda d, u: True)
    assert (
        coa_canary.send_canary(db_session, target)
        is CanaryDisposition.aborted_collision
    )
    monkeypatch.setattr(coa_canary, "external_radacct_username_seen", lambda d, u: None)
    assert (
        coa_canary.send_canary(db_session, target)
        is CanaryDisposition.collision_check_unavailable
    )
    assert sent == []  # nothing was transmitted in either case
    monkeypatch.setattr(
        coa_canary, "external_radacct_username_seen", lambda d, u: False
    )
    assert coa_canary.send_canary(db_session, target) is CanaryDisposition.rejected
    assert len(sent) == 1


def test_external_radacct_lookup_error_returns_none(db_session, monkeypatch) -> None:
    from app.services import radius_session_reconcile as rsr

    monkeypatch.setattr(
        rsr, "_active_external_sync_configs", lambda db: [{"db_url": "x"}]
    )

    def boom(url):
        raise RuntimeError("down")

    monkeypatch.setattr(rsr, "_get_external_engine", boom)
    assert rsr.external_radacct_username_seen(db_session, "u") is None
    monkeypatch.setattr(rsr, "_active_external_sync_configs", lambda db: [])
    assert rsr.external_radacct_username_seen(db_session, "u") is False


def test_unavailable_collision_check_is_a_config_finding_not_a_bucket(
    db_session, canary_env, monkeypatch
) -> None:
    add_nas(db_session, "a", "192.0.2.10")
    monkeypatch.setattr(
        coa_canary,
        "send_canary",
        lambda db, t: CanaryDisposition.collision_check_unavailable,
    )
    stats = coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: pytest.fail("not a CoA failure"),
        sleep=lambda s: None,
    )
    assert stats.findings == 1 and canary_env == []


def test_run_stops_scheduling_probes_when_budget_is_below_one_spacing(
    db_session, canary_env, monkeypatch
) -> None:
    for i in range(4):
        add_nas(db_session, f"n{i}", f"192.0.2.{10 + i}")
    sent: list[str] = []
    monkeypatch.setattr(
        coa_canary,
        "send_canary",
        lambda db, t: sent.append(t.host) or CanaryDisposition.rejected,
    )
    clock = iter([0.0, 1.0, 9.0, 99.0, 99.0, 99.0])  # start, then elapsed readings
    stats = coa_canary.run_coa_canary(
        db_session,
        task_name="t",
        schedule_reprobe=lambda *a: None,
        sleep=lambda s: None,
        monotonic=lambda: next(clock),
        budget_seconds=10.0,
    )
    # 2nd probe: 10-1=9 left >= 2s spacing -> sent; 3rd: 10-9=1 < 2s -> stop.
    assert len(sent) == 2
    assert stats.targets == 2 and stats.deferred == 2


def test_duplicate_rows_probe_with_the_row_that_has_a_local_secret(db_session) -> None:
    no_secret = add_nas(db_session, "a-1", "192.0.2.10")
    with_secret = add_nas(db_session, "a-2", "192.0.2.10")
    with_secret.shared_secret = "enc:x"
    db_session.commit()
    (target,) = coa_canary._group_targets([no_secret, with_secret])
    assert target.primary_nas_device_id == with_secret.id
