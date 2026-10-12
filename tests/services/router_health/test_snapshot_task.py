from __future__ import annotations

import types
from datetime import UTC, datetime
from uuid import uuid4

import paramiko
import pytest
from paramiko.ssh_exception import (
    AuthenticationException,
    BadHostKeyException,
    NoValidConnectionsError,
)

from app.models.router_health import RouterHealthSignal
from app.models.router_management import Router, RouterStatus
from app.services.router_health import commands, reconciler
from app.services.router_health.contracts import (
    ConfigExportFailureClass as C,
)
from app.services.router_health.contracts import ConfigExportOutcome
from app.services.router_health.export_outcomes import classify_export_failure
from app.services.router_management import config_export as ce
from app.tasks import router_sync


def wrapped(cause: BaseException) -> ce.RouterConfigExportError:
    try:
        raise ce.RouterConfigExportError("SENTINEL-TEXT") from cause
    except ce.RouterConfigExportError as exc:
        return exc


@pytest.mark.parametrize(
    "exc,expected",
    [
        (wrapped(AuthenticationException("x")), C.auth_failed),
        (wrapped(BadHostKeyException("h", None, None)), C.host_key_changed),
        (
            wrapped(paramiko.SSHException("Server 'h' not found in known hosts")),
            C.host_key_unknown,
        ),
        (wrapped(NoValidConnectionsError({("h", 120): OSError()})), C.unreachable),
        (wrapped(TimeoutError()), C.unreachable),
        (wrapped(TimeoutError()), C.unreachable),
        (ce.RouterConfigExportEmptyError("e"), C.empty_export),
        (ce.RouterConfigExportNoAuthError("n"), C.no_auth_configured),
        (wrapped(ValueError("weird")), C.other),
    ],
)
def test_failure_classification(exc, expected) -> None:
    assert classify_export_failure(exc) is expected


def add_router(db, name, status):
    router = Router(
        name=name,
        hostname=name,
        management_ip="192.0.2.20",
        rest_api_username="u",
        rest_api_password="p",
        status=status,
        is_active=True,
    )
    db.add(router)
    db.commit()
    return router


def test_widened_scope_strictness_and_single_outcome_call(
    db_session, monkeypatch
) -> None:
    online = add_router(db_session, "online", RouterStatus.online)
    offline = add_router(db_session, "offline", RouterStatus.unreachable)
    add_router(db_session, "maint", RouterStatus.maintenance)
    calls: list[tuple[str, bool]] = []

    def fake_fetch(router, *, strict_host_key=False):
        calls.append((router.name, strict_host_key))
        if router.name == "offline":
            raise wrapped(paramiko.SSHException("Server 'h' not found in known hosts"))
        return "/system identity\nset name=online\n"

    recorded: list[list[ConfigExportOutcome]] = []
    monkeypatch.setattr(router_sync, "_fetch_config_export", fake_fetch)
    monkeypatch.setattr(
        router_sync.db_session_adapter, "create_session", lambda: db_session
    )
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(
        router_sync.router_health_reconciler,
        "record_config_export_outcomes",
        lambda db, outcomes, task_name: recorded.append(outcomes),
    )
    monkeypatch.setattr(
        router_sync.RouterConfigService, "store_snapshot", lambda *a, **k: None
    )
    monkeypatch.setattr(
        router_sync.control_registry, "is_enabled", lambda db, key: False
    )
    result = router_sync.capture_scheduled_snapshots()
    assert dict(calls) == {"online": False, "offline": True}  # maintenance excluded
    assert result["total"] == 2 and result["failed"] == 1
    assert len(recorded) == 1
    by_id = {o.router_id: o for o in recorded[0]}
    assert by_id[online.id].ok
    assert by_id[offline.id].failure_class is C.host_key_unknown


def test_outcomes_store_class_only_and_count_failures(db_session) -> None:
    router = add_router(db_session, "r", RouterStatus.online)
    rid = router.id
    db_session.commit()
    now = datetime(2026, 10, 12, tzinfo=UTC)
    fail = ConfigExportOutcome(rid, False, C.auth_failed, now)
    for _ in range(2):
        db_session.rollback()
        reconciler.record_config_export_outcomes(
            db_session, outcomes=[fail], task_name="t"
        )
    row = db_session.query(RouterHealthSignal).one()
    assert row.status == "abnormal" and row.last_text == "auth_failed"
    assert row.consecutive_abnormal == 2 and row.first_abnormal_at is not None
    db_session.rollback()
    reconciler.record_config_export_outcomes(
        db_session, outcomes=[ConfigExportOutcome(rid, True, None, now)], task_name="t"
    )
    row = db_session.query(RouterHealthSignal).one()
    assert (
        row.status == "ok" and row.last_text is None and row.consecutive_abnormal == 0
    )
    assert len(row.samples) == 3


def test_ring_buffer_is_bounded(db_session) -> None:
    router = add_router(db_session, "r", RouterStatus.online)
    rid = router.id
    db_session.commit()
    now = datetime(2026, 10, 12, tzinfo=UTC)
    outcomes = [ConfigExportOutcome(rid, True, None, now) for _ in range(5)]
    for _ in range(45):
        db_session.rollback()
        reconciler.record_config_export_outcomes(
            db_session, outcomes=outcomes, task_name="t"
        )
    assert (
        len(db_session.query(RouterHealthSignal).one().samples) == commands.SAMPLE_LIMIT
    )


def test_fetch_passes_strict_flag_only_when_requested(monkeypatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(
        ce, "settings", types.SimpleNamespace(router_config_export_via_ssh=True)
    )
    monkeypatch.setattr(
        ce, "export_config_via_ssh", lambda router, **kw: seen.append(kw) or "x"
    )
    ce.fetch_config_export(object())
    ce.fetch_config_export(object(), strict_host_key=True)
    assert seen == [{}, {"strict_host_key": True}]


def test_strict_flag_forces_reject_policy_even_when_global_setting_is_off(
    monkeypatch,
) -> None:
    created: dict = {}

    class Client:
        def load_host_keys(self, path):
            pass

        def set_missing_host_key_policy(self, policy):
            created["policy"] = type(policy).__name__

    monkeypatch.setattr(
        ce,
        "settings",
        types.SimpleNamespace(
            router_config_ssh_known_hosts_path="",
            router_config_ssh_strict_host_key=False,
        ),
    )
    ce._install_host_key_policy(Client(), strict=True)
    assert created["policy"] == "RejectPolicy"
    ce._install_host_key_policy(Client(), strict=False)
    assert created["policy"] == "AutoAddPolicy"


def test_posture_run_end_to_end_then_fix_resolves(db_session, monkeypatch) -> None:
    from app.models.admin_alert import AdminAlert
    from app.models.network_monitoring import AlertStatus
    from app.models.router_management import RouterConfigSnapshot, RouterSnapshotSource
    from tests.services.router_health.support import build_export

    router = add_router(db_session, "R1", RouterStatus.online)

    def snapshot(text):
        db_session.add(
            RouterConfigSnapshot(
                router_id=router.id,
                config_export=text,
                config_hash="h",
                source=RouterSnapshotSource.scheduled,
                created_at=datetime.now(UTC),
            )
        )
        db_session.commit()

    snapshot(build_export(ntp=None))
    stats = reconciler.reconcile_router_posture(db_session, task_name="t")
    assert stats.subjects == 1
    open_checks = {
        a.fingerprint.split(":")[4]
        for a in db_session.query(AdminAlert).filter(
            AdminAlert.status == AlertStatus.open
        )
    }
    assert "ntp-client-off" in open_checks
    snapshot(build_export())
    reconciler.reconcile_router_posture(
        db_session, task_name="t", now=datetime.now(UTC)
    )
    db_session.expire_all()
    still = [
        a.fingerprint
        for a in db_session.query(AdminAlert).filter(
            AdminAlert.status == AlertStatus.open
        )
        if ":ntp-client-off:" in a.fingerprint
    ]
    assert still == []
    assert uuid4()  # keep import used
