from __future__ import annotations

from contextlib import contextmanager

from app.models.network import DeviceStatus, OLTDevice
from app.services.network import ont_runtime_status
from app.services.queue_adapter import QueueDispatchResult
from app.tasks import ont_runtime_status as ont_runtime_status_tasks


def _olt(db_session, *, name: str, vendor: str, **values) -> OLTDevice:
    olt = OLTDevice(
        name=name,
        vendor=vendor,
        is_active=values.pop("is_active", True),
        status=values.pop("status", DeviceStatus.active),
        **values,
    )
    db_session.add(olt)
    db_session.flush()
    return olt


def test_huawei_bulk_pollability_has_one_canonical_predicate(db_session):
    pollable = _olt(db_session, name="Pollable Huawei", vendor="HUAWEI")
    inactive = _olt(
        db_session,
        name="Inactive Huawei",
        vendor="Huawei",
        is_active=False,
    )
    uisp = _olt(
        db_session,
        name="UISP Huawei",
        vendor="Huawei",
        uisp_device_id="uisp-huawei-1",
    )
    other = _olt(db_session, name="Other Vendor", vendor="ZTE")

    assert ont_runtime_status.huawei_olt_status_pollable(pollable) is True
    assert ont_runtime_status.huawei_olt_status_pollable(inactive) is False
    assert ont_runtime_status.huawei_olt_status_pollable(uisp) is False
    assert ont_runtime_status.huawei_olt_status_pollable(other) is False


def test_runtime_status_owner_publishes_bulk_observation_poll(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_enqueue(task_name: str, **kwargs) -> QueueDispatchResult:
        calls.append((task_name, kwargs))
        return QueueDispatchResult(
            queued=True,
            task_name=task_name,
            queue=kwargs.get("queue"),
        )

    monkeypatch.setattr(ont_runtime_status, "enqueue_task", fake_enqueue)

    result = ont_runtime_status.queue_huawei_olt_status_poll(
        "olt-1", source="network.ont_status_refresh"
    )

    assert result.queued is True
    assert calls == [
        (
            "app.tasks.ont_runtime_status.refresh_huawei_olt_status",
            {
                "args": ["olt-1"],
                "queue": "ingestion",
                "correlation_id": "ont-status-refresh:olt-1",
                "source": "network.ont_status_refresh",
            },
        )
    ]


def test_scheduled_dispatch_uses_shared_pollability_and_queue_owner(
    db_session, monkeypatch
):
    pollable = _olt(db_session, name="Scheduled Huawei", vendor="Huawei")
    _olt(
        db_session,
        name="Scheduled Inactive Huawei",
        vendor="Huawei",
        is_active=False,
    )
    _olt(
        db_session,
        name="Scheduled UISP Huawei",
        vendor="Huawei",
        uisp_device_id="uisp-huawei-2",
    )
    _olt(db_session, name="Scheduled Other Vendor", vendor="ZTE")

    @contextmanager
    def session():
        yield db_session

    queued: list[tuple[str, str]] = []

    def fake_queue(olt_id: str, *, source: str) -> QueueDispatchResult:
        queued.append((olt_id, source))
        return QueueDispatchResult(
            queued=True,
            task_name="app.tasks.ont_runtime_status.refresh_huawei_olt_status",
            queue="ingestion",
        )

    monkeypatch.setattr(ont_runtime_status_tasks.db_session_adapter, "session", session)
    monkeypatch.setattr(
        ont_runtime_status,
        "queue_huawei_olt_status_poll",
        fake_queue,
    )

    result = ont_runtime_status_tasks.dispatch_huawei_ont_status.run()

    assert result == {"queued": 1, "failed": 0}
    assert queued == [
        (str(pollable.id), "network.ont_runtime_status.scheduled"),
    ]


def test_worker_rechecks_pollability_before_device_io(db_session, monkeypatch):
    olt = _olt(
        db_session,
        name="Execution-time UISP Huawei",
        vendor="Huawei",
        uisp_device_id="uisp-huawei-3",
    )

    @contextmanager
    def session():
        yield db_session

    @contextmanager
    def acquired(_lock_key: int):
        yield True

    monkeypatch.setattr(ont_runtime_status_tasks.db_session_adapter, "session", session)
    monkeypatch.setattr(
        ont_runtime_status_tasks,
        "postgres_session_advisory_lock",
        acquired,
    )

    result = ont_runtime_status_tasks.refresh_huawei_olt_status.run(str(olt.id))

    assert result == {"olt_id": str(olt.id), "skipped": "not_pollable"}
