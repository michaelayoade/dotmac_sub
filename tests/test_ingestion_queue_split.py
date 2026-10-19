"""Ingestion queue split (RCA 2026-10-19): routing, compose and fan-out expiry.

One ``--concurrency=1`` worker served ~35 task types; long inventory jobs kept
it busy so minute-cadence tasks expired in the queue ("Discarding revoked
task"). Inventory collection now has its own queue and worker.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.celery_app import celery_app
from app.services import scheduler_config
from app.services.scheduler_config import fanout_child_expires_seconds

ROOT = Path(__file__).resolve().parents[1]

INVENTORY_TASKS = {
    "app.tasks.olt_mac_harvest.run_olt_mac_harvest",
    "app.tasks.olt_mac_harvest.run_single_olt_mac_harvest",
    "app.tasks.topology_uisp.run_uisp_topology_sync",
    "app.tasks.ont_runtime_status.dispatch_huawei_ont_status",
    "app.tasks.ont_runtime_status.refresh_huawei_olt_status",
    "app.tasks.ont_runtime_status.refresh_single_ont_status",
    "app.tasks.topology_lldp.run_lldp_topology_poll",
    "app.tasks.topology_ufiber_link.run_ufiber_onu_link",
    "app.tasks.router_health.observe_router_runtime",
}
MINUTE_CADENCE_INGESTION_TASKS = {
    "app.tasks.usage.import_radius_accounting",
    "app.tasks.usage.meter_usage_into_quota",
    "app.tasks.radius.reconcile_active_sessions",
    "app.tasks.topology_outage.reconcile_detected_outages",
    "app.tasks.customer_impact_metrics.export_customer_impact_metrics",
}


def _route(task: str) -> str:
    return celery_app.conf.task_routes[task]["queue"]


def test_inventory_queue_is_declared() -> None:
    declared = {queue.name for queue in celery_app.conf.task_queues}
    assert {"ingestion", "inventory", "monitoring"} <= declared


@pytest.mark.parametrize("task", sorted(INVENTORY_TASKS))
def test_inventory_collection_routes_to_inventory(task: str) -> None:
    assert _route(task) == "inventory"


@pytest.mark.parametrize("task", sorted(MINUTE_CADENCE_INGESTION_TASKS))
def test_minute_cadence_tasks_stay_on_ingestion(task: str) -> None:
    assert _route(task) == "ingestion"


def test_radius_health_probe_runs_on_the_monitoring_queue() -> None:
    assert _route("app.tasks.radius_health.run_radius_health_check") == "monitoring"


def _service_block(compose: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [\w-]+:\n|^\S|\Z)", compose
    )
    assert match is not None, name
    return match.group("body")


def test_compose_ingestion_concurrency_lives_in_the_base_file() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = _service_block(compose, "celery-worker-ingestion")
    assert "\n    - --concurrency=3\n" in worker
    assert "\n    - ingestion\n" in worker
    assert "DB_APPLICATION_NAME: dotmac_sub_celery_worker_ingestion" in worker


def test_compose_declares_a_dedicated_inventory_worker() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = _service_block(compose, "celery-worker-inventory")
    assert "image: ${APP_IMAGE" in worker
    assert "\n    - inventory\n" in worker
    assert "\n    - ingestion\n" not in worker
    assert "DB_APPLICATION_NAME: dotmac_sub_celery_worker_inventory" in worker


def test_monitoring_worker_can_run_the_radius_probe() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()
    worker = _service_block(compose, "celery-worker-monitoring")
    for name in ("RADIUS_PROBE_SECRET", "RADIUS_PROBE_PASSWORD", "RADIUS_PROBE_HOST"):
        assert f"      {name}:" in worker


class _Query:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def filter(self, *_a: object) -> _Query:
        return self

    def order_by(self, *_a: object) -> _Query:
        return self

    def first(self) -> object | None:
        return self._row


class _Db:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def query(self, *_a: object) -> _Query:
        return _Query(self._row)


def test_fanout_child_expires_equals_parent_interval() -> None:
    db = _Db(SimpleNamespace(interval_seconds=1800))
    assert (
        fanout_child_expires_seconds(db, "parent", default_interval_seconds=60) == 1800
    )


def test_fanout_child_expiry_uses_default_without_a_schedule_row() -> None:
    assert (
        fanout_child_expires_seconds(_Db(None), "parent", default_interval_seconds=300)
        == 300
    )


def test_fanout_child_expiry_matches_the_periodic_entry_rule() -> None:
    # Day-or-longer parents use the same 12h cap as the periodic entry itself.
    db = _Db(SimpleNamespace(interval_seconds=86400))
    assert fanout_child_expires_seconds(
        db, "parent", default_interval_seconds=60
    ) == scheduler_config._entry_expires_seconds(86400)


def test_fanout_expiry_lookup_failure_never_blocks_the_fanout() -> None:
    class _Broken:
        def query(self, *_a: object) -> object:
            raise RuntimeError("db down")

    assert (
        fanout_child_expires_seconds(_Broken(), "parent", default_interval_seconds=300)
        == 300
    )


def test_olt_mac_harvest_children_are_enqueued_with_expires(monkeypatch) -> None:
    from contextlib import contextmanager
    from uuid import uuid4

    from app.tasks import olt_mac_harvest

    olt_ids = [uuid4(), uuid4()]

    class _Scalars:
        def all(self) -> list[object]:
            return olt_ids

    class _ReadDb(_Db):
        def scalars(self, *_a: object) -> _Scalars:
            return _Scalars()

    @contextmanager
    def read_session():
        yield _ReadDb(SimpleNamespace(interval_seconds=1200))

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        olt_mac_harvest.db_session_adapter, "read_session", read_session
    )
    monkeypatch.setattr(
        olt_mac_harvest,
        "enqueue_celery_task",
        lambda _task, **kwargs: calls.append(kwargs),
    )

    result = olt_mac_harvest.run_olt_mac_harvest.run()

    assert result == {"olts": 2, "queued": 2, "failed": 0}
    assert [call["expires"] for call in calls] == [1200, 1200]


def test_queue_adapter_passes_expires_to_apply_async() -> None:
    from app.services.queue_adapter import CeleryQueueAdapter, QueueMessage

    seen: dict[str, object] = {}

    def fake_enqueue(task_name: str, **kwargs: object) -> object:
        seen.update(kwargs)
        return SimpleNamespace(id="t1")

    adapter = CeleryQueueAdapter(fake_enqueue)
    result = adapter.enqueue(
        QueueMessage(task_name="x", queue="inventory", expires=300)
    )
    assert result.queued is True
    assert seen["expires"] == 300 and seen["queue"] == "inventory"


def test_application_name_is_sanitised_and_optional() -> None:
    from app.db import resolve_application_name

    assert resolve_application_name("") is None
    assert resolve_application_name("   ") is None
    assert (
        resolve_application_name("dotmac_sub_celery_worker_inventory")
        == "dotmac_sub_celery_worker_inventory"
    )
    assert resolve_application_name("bad name;drop") == "bad_name_drop"
    assert len(resolve_application_name("x" * 200) or "") == 63


def test_status_dispatch_lookup_failure_cannot_fail_the_fanout(monkeypatch) -> None:
    from contextlib import contextmanager
    from uuid import uuid4

    from app.services.network import ont_runtime_status
    from app.services.queue_adapter import QueueDispatchResult
    from app.tasks import ont_runtime_status as status_tasks

    olt_id = uuid4()

    class _Broken:
        def query(self, *_a: object) -> object:
            raise RuntimeError("lookup session poisoned")

    class _Scalars:
        def all(self) -> list[object]:
            return [olt_id]

    class _MainDb:
        def query(self, *_a: object) -> object:  # would fail if shared
            raise AssertionError("fan-out session must not run the lookup")

        def scalars(self, *_a: object) -> _Scalars:
            return _Scalars()

    @contextmanager
    def read_session():
        yield _Broken()

    @contextmanager
    def session():
        yield _MainDb()

    expiries: list[int | None] = []

    def fake_queue(olt: str, *, source: str, expires: int | None = None):
        expiries.append(expires)
        return QueueDispatchResult(queued=True)

    monkeypatch.setattr(status_tasks.db_session_adapter, "read_session", read_session)
    monkeypatch.setattr(status_tasks.db_session_adapter, "session", session)
    monkeypatch.setattr(ont_runtime_status, "queue_huawei_olt_status_poll", fake_queue)

    result = status_tasks.dispatch_huawei_ont_status.run()

    assert result == {"queued": 1, "failed": 0}
    assert expiries == [300]  # documented default, fan-out unaffected


@pytest.mark.parametrize(
    "service", ["celery-worker-ingestion", "celery-worker-inventory"]
)
def test_compose_caps_worker_db_pool(service: str) -> None:
    worker = _service_block((ROOT / "docker-compose.yml").read_text(), service)
    assert '      DB_POOL_SIZE: "2"\n' in worker
    assert '      DB_MAX_OVERFLOW: "1"\n' in worker


def test_inventory_worker_has_no_object_storage_credentials() -> None:
    worker = _service_block(
        (ROOT / "docker-compose.yml").read_text(), "celery-worker-inventory"
    )
    assert "S3_" not in worker
