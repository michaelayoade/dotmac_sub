"""Runtime poll adapter: persistence, scoped resolution, carry-forward, bounds."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.admin_alert import AdminAlert, AdminNotification
from app.models.network_monitoring import AlertStatus
from app.models.rbac import Role, SystemUserRole
from app.models.router_health import RouterHealthSignal
from app.models.router_management import Router, RouterStatus
from app.models.system_user import SystemUser
from app.services.router_health import runtime_poll
from app.services.router_health.contracts import RestFailure, RuntimeObservation
from app.services.router_health.policy import OpsUserPolicy, RestUserPolicy
from app.tasks import router_health as tasks

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class ScriptedReader:
    """Return a scripted observation per router id; track concurrency."""

    def __init__(self, script, delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.optics_requested: dict[uuid.UUID, bool] = {}
        self._lock = threading.Lock()

    def poll(self, router, *, router_id, include_optics):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.optics_requested[router_id] = include_optics
        try:
            time.sleep(self.delay)
            produce = self.script[router_id]
            return produce() if callable(produce) else produce
        finally:
            with self._lock:
                self.active -= 1


def add_router(db, name, *, status=RouterStatus.online, active=True, has_secret=True):
    router = Router(
        name=name,
        hostname=name,
        management_ip="192.0.2.20",
        rest_api_username="rest-api",
        rest_api_password="p" if has_secret else "",
        status=status,
        is_active=active,
    )
    db.add(router)
    db.commit()
    return router


def healthy(router_id, **extra) -> RuntimeObservation:
    base = {
        "router_id": router_id,
        "observed_at": NOW,
        "resource": {"uptime": "3d", "version": "7.16.2"},
        "ppp_active_count": 120,
    }
    base.update(extra)
    return RuntimeObservation(**base)


def failing(router_id, failure) -> RuntimeObservation:
    return RuntimeObservation(router_id=router_id, observed_at=NOW, failure=failure)


@pytest.fixture(autouse=True)
def controls(monkeypatch):
    monkeypatch.setattr(
        runtime_poll.control_registry,
        "is_enabled",
        lambda db, key: key == runtime_poll.CONTROL_OPTICS and OPTICS["on"],
    )
    OPTICS["on"] = False
    yield


OPTICS = {"on": False}


def open_alerts(db):
    return {
        a.fingerprint: a
        for a in db.query(AdminAlert).filter(AdminAlert.status != AlertStatus.resolved)
    }


def test_poll_writes_signals_and_findings_in_shadow_mode(db_session) -> None:
    router = add_router(db_session, "rt-a")
    admin = SystemUser(first_name="A", last_name="B", email="rh-poll@example.com")
    role = Role(name="admin", is_active=True)
    db_session.add_all([admin, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=admin.id, role_id=role.id))
    db_session.commit()
    reader = ScriptedReader({router.id: failing(router.id, RestFailure.auth_failed)})

    stats = runtime_poll.poll_router_runtime(
        db_session, task_name="t", now=NOW, reader=reader
    )

    assert (stats.polled, stats.failed, stats.skipped) == (1, 1, 0)
    fps = open_alerts(db_session)
    assert f"network:router-health:runtime:{router.id}:rest-auth-failed:0" in fps
    alert = next(iter(fps.values()))
    assert alert.details["priority"] == "high"
    assert alert.details["mode"] == "shadow"
    assert alert.target_url.endswith("#health")
    assert db_session.query(AdminNotification).count() == 0  # shadow: no paging
    signal = db_session.query(RouterHealthSignal).one()
    assert (signal.signal, signal.status, signal.last_text) == (
        "rest_access",
        "abnormal",
        "auth_failed",
    )


def test_recovery_resolves_the_access_finding(db_session) -> None:
    router = add_router(db_session, "rt-a")
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW,
        reader=ScriptedReader({router.id: failing(router.id, RestFailure.auth_failed)}),
    )
    assert open_alerts(db_session)
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW + timedelta(minutes=15),
        reader=ScriptedReader({router.id: healthy(router.id)}),
    )
    assert not [fp for fp in open_alerts(db_session) if ":rest-auth-failed:" in fp]


def test_unreadable_router_carries_other_runtime_findings_forward(db_session) -> None:
    router = add_router(db_session, "rt-a")
    stale_clock = {
        "date": "2026-02-01",
        "time": "00:00:00",
        "gmt-offset": "+00:00",
    }
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW,
        reader=ScriptedReader(
            {router.id: healthy(router.id, clock=stale_clock, server_time_utc=NOW)}
        ),
    )
    skew_fp = f"network:router-health:runtime:{router.id}:clock-skew:0"
    assert skew_fp in open_alerts(db_session)
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW + timedelta(minutes=15),
        reader=ScriptedReader({router.id: failing(router.id, RestFailure.unreachable)}),
    )
    assert skew_fp in open_alerts(db_session)  # not resolved by a failed poll


def test_cleared_condition_resolves_only_its_own_check(db_session) -> None:
    router = add_router(db_session, "rt-a")
    bad = healthy(
        router.id,
        clock={"date": "2026-02-01", "time": "00:00:00", "gmt-offset": "+00:00"},
        server_time_utc=NOW,
        ntp={"enabled": "false"},
    )
    runtime_poll.poll_router_runtime(
        db_session, task_name="t", now=NOW, reader=ScriptedReader({router.id: bad})
    )
    good_clock = healthy(
        router.id,
        clock={"date": "2026-10-13", "time": "12:15:00", "gmt-offset": "+00:00"},
        server_time_utc=NOW + timedelta(minutes=15),
        ntp={"enabled": "false"},
    )
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW + timedelta(minutes=15),
        reader=ScriptedReader({router.id: good_clock}),
    )
    fps = open_alerts(db_session)
    assert f"network:router-health:runtime:{router.id}:clock-skew:0" not in fps


def test_only_active_non_maintenance_routers_with_credentials_are_polled(
    db_session,
) -> None:
    ok = add_router(db_session, "rt-ok")
    add_router(db_session, "rt-maint", status=RouterStatus.maintenance)
    add_router(db_session, "rt-inactive", active=False)
    add_router(db_session, "rt-nopass", has_secret=False)
    unreachable = add_router(db_session, "rt-dead", status=RouterStatus.unreachable)
    reader = ScriptedReader(
        {ok.id: healthy(ok.id), unreachable.id: healthy(unreachable.id)}
    )
    stats = runtime_poll.poll_router_runtime(
        db_session, task_name="t", now=NOW, reader=reader
    )
    # Router.status is not a gate: an "unreachable" router is still polled.
    assert stats.polled == 2
    assert stats.skipped == 1
    assert set(reader.optics_requested) == {ok.id, unreachable.id}


def test_runtime_layer_never_writes_router_status(db_session) -> None:
    router = add_router(db_session, "rt-a", status=RouterStatus.offline)
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW,
        reader=ScriptedReader({router.id: healthy(router.id)}),
    )
    db_session.expire_all()
    refreshed = db_session.get(Router, router.id)
    assert refreshed.status is RouterStatus.offline
    assert refreshed.last_seen_at is None


def test_concurrency_is_bounded_by_the_setting(db_session, monkeypatch) -> None:
    routers = [add_router(db_session, f"rt-{i}") for i in range(6)]
    reader = ScriptedReader({r.id: healthy(r.id) for r in routers}, delay=0.05)
    monkeypatch.setattr(
        runtime_poll,
        "load_settings",
        lambda db: runtime_poll.RouterHealthSettings(runtime_concurrency=2),
    )
    runtime_poll.poll_router_runtime(db_session, task_name="t", now=NOW, reader=reader)
    assert 1 <= reader.max_active <= 2


def test_optics_requested_only_when_control_on_and_hourly(db_session) -> None:
    router = add_router(db_session, "rt-a")
    off = ScriptedReader({router.id: healthy(router.id)})
    runtime_poll.poll_router_runtime(db_session, task_name="t", now=NOW, reader=off)
    assert off.optics_requested[router.id] is False
    OPTICS["on"] = True
    on = ScriptedReader({router.id: healthy(router.id)})
    runtime_poll.poll_router_runtime(
        db_session, task_name="t", now=NOW + timedelta(minutes=15), reader=on
    )
    assert on.optics_requested[router.id] is True


def test_signal_ring_buffer_and_unique_rows_across_runs(db_session) -> None:
    router = add_router(db_session, "rt-a")
    for i in range(3):
        runtime_poll.poll_router_runtime(
            db_session,
            task_name="t",
            now=NOW + timedelta(minutes=15 * i),
            reader=ScriptedReader({router.id: healthy(router.id)}),
        )
    rows = db_session.query(RouterHealthSignal).filter_by(signal="ppp_active").all()
    assert len(rows) == 1
    assert len(rows[0].samples) == 3


def test_findings_for_deactivated_routers_are_retired(db_session) -> None:
    router = add_router(db_session, "rt-a")
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW,
        reader=ScriptedReader({router.id: failing(router.id, RestFailure.auth_failed)}),
    )
    assert open_alerts(db_session)
    db_session.get(Router, router.id).is_active = False
    db_session.commit()
    runtime_poll.poll_router_runtime(
        db_session,
        task_name="t",
        now=NOW + timedelta(minutes=15),
        reader=ScriptedReader({}),
    )
    assert not open_alerts(db_session)


def test_policy_users_flow_through_to_findings(db_session, monkeypatch) -> None:
    router = add_router(db_session, "rt-a")
    from app.services.router_health import policy as policy_mod

    base = policy_mod.built_in_defaults().model_copy(
        update={
            "ops_user": OpsUserPolicy(
                name="ops-snap", group="read", required_address="192.0.2.50"
            ),
            "rest_user": RestUserPolicy(name="rest-api", forbidden_groups=["full"]),
        }
    )
    monkeypatch.setattr(
        runtime_poll,
        "load_policy",
        lambda db: policy_mod.LoadedPolicy(
            policy=policy_mod.RouterHealthPolicy(defaults=base)
        ),
    )
    obs = healthy(
        router.id,
        users=(
            {"name": "rest-api", "group": "read", "address": "", "disabled": "false"},
        ),
    )
    runtime_poll.poll_router_runtime(
        db_session, task_name="t", now=NOW, reader=ScriptedReader({router.id: obs})
    )
    assert f"network:router-health:runtime:{router.id}:ops-user:missing" in open_alerts(
        db_session
    )


def test_task_adapter_gates_on_its_control_and_calls_one_function(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tasks.control_registry, "is_enabled", lambda db, key: False)
    assert tasks.poll_router_runtime.run() == {"skipped": "control_disabled"}
    assert tasks.TASK_RUNTIME == "app.tasks.router_health.poll_router_runtime"
    assert tasks.SOFT_LIMIT_SECONDS[tasks.TASK_RUNTIME] <= 8 * 60
