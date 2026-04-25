"""Tests for bulk direct ONT provisioning dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID


class _SessionContext:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        return False


def test_bulk_provision_action_queues_direct_orchestrator(monkeypatch):
    from app.models.network import BulkProvisioningRunStatus
    from app.services.network.bulk_provisioning import BulkProvisioningDispatchResult
    from app.tasks.ont_bulk import execute_bulk_action

    calls: list[dict[str, Any]] = []

    def fake_bulk_provision_onts(db, ont_ids, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"ont_ids": ont_ids, **kwargs})
        return BulkProvisioningDispatchResult(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            correlation_key="bulk-test",
            status=BulkProvisioningRunStatus.running,
            total=2,
            queued=2,
            skipped=0,
            orchestrator_task_id="bulk-orchestrator-task",
        )

    monkeypatch.setattr(
        "app.services.network.bulk_provisioning.bulk_provision_onts",
        fake_bulk_provision_onts,
    )

    result = execute_bulk_action.run(
        ["ont-a", "ont-b"],
        "provision",
        {
            "tr069_olt_profile_id": "tr069-profile-1",
            "initiated_by": "admin",
            "max_parallel": 10,
            "chunk_delay_seconds": 20,
        },
    )

    assert result == {
        "processed": 0,
        "errors": 0,
        "skipped": 0,
        "queued": 2,
        "bulk_run_id": "00000000-0000-0000-0000-000000000001",
        "correlation_key": "bulk-test",
        "orchestrator_task_id": "bulk-orchestrator-task",
        "provisioning_mode": "direct",
    }
    assert calls == [
        {
            "ont_ids": ["ont-a", "ont-b"],
            "tr069_olt_profile_id": "tr069-profile-1",
            "max_workers": 10,
            "chunk_delay_seconds": 20,
            "initiated_by": "admin",
            "correlation_key": None,
            "dry_run": False,
            "allow_low_optical_margin": False,
            "step_data": {},
            "metadata": {"source": "ont_bulk_action"},
        }
    ]


def test_bulk_direct_orchestrator_dedupes_and_chunks(monkeypatch):
    import app.celery_app as celery_module
    from app.tasks.ont_provisioning import queue_bulk_provisioning

    enqueued: list[tuple[Any, dict[str, Any]]] = []

    def fake_enqueue(task_or_name, **kwargs):  # type: ignore[no-untyped-def]
        enqueued.append((task_or_name, kwargs))
        return SimpleNamespace(id=f"provision-task-{len(enqueued)}")

    monkeypatch.setattr(celery_module, "enqueue_celery_task", fake_enqueue)

    result = queue_bulk_provisioning.run(
        ["ont-1", "ont-2", "ont-1", "", "ont-3"],
        dry_run=True,
        initiated_by="admin",
        max_parallel=2,
        chunk_delay_seconds=30,
    )

    assert result["queued"] == 3
    assert result["skipped"] == 2
    assert result["errors"] == 0
    assert result["chunks"] == 2
    assert [task["ont_id"] for task in result["tasks"]] == ["ont-1", "ont-2", "ont-3"]
    assert [task["countdown"] for task in result["tasks"]] == [0, 0, 30]

    assert len(enqueued) == 3
    assert [kwargs["countdown"] for _, kwargs in enqueued] == [0, 0, 30]
    assert [
        kwargs["kwargs"]["ont_id"] for _, kwargs in enqueued
    ] == ["ont-1", "ont-2", "ont-3"]
    assert all(
        task_or_name == "app.tasks.ont_provisioning.provision_ont"
        for task_or_name, _ in enqueued
    )


def test_bulk_direct_orchestrator_uses_bulk_item_correlation(
    db_session,
    monkeypatch,
):
    import app.celery_app as celery_module
    from app.models.network import (
        BulkProvisioningItem,
        BulkProvisioningItemStatus,
        BulkProvisioningRun,
        BulkProvisioningRunStatus,
        OntUnit,
    )
    from app.tasks.ont_provisioning import queue_bulk_provisioning
    import app.tasks.ont_provisioning as provisioning_task_module

    ont = OntUnit(serial_number="BULK-DIRECT-CORR")
    db_session.add(ont)
    db_session.commit()
    db_session.refresh(ont)

    run = BulkProvisioningRun(
        status=BulkProvisioningRunStatus.running,
        correlation_key="bulk-corr",
        total_count=1,
    )
    db_session.add(run)
    db_session.flush()
    item = BulkProvisioningItem(
        run_id=run.id,
        requested_ont_id=str(ont.id),
        ont_unit_id=ont.id,
        status=BulkProvisioningItemStatus.pending,
        correlation_key=f"bulk-corr:ont:{ont.id}",
    )
    db_session.add(item)
    db_session.commit()

    enqueued: list[tuple[Any, dict[str, Any]]] = []

    def fake_enqueue(task_or_name, **kwargs):  # type: ignore[no-untyped-def]
        enqueued.append((task_or_name, kwargs))
        return SimpleNamespace(id="provision-task-1")

    monkeypatch.setattr(celery_module, "enqueue_celery_task", fake_enqueue)
    monkeypatch.setattr(
        provisioning_task_module.db_session_adapter,
        "read_session",
        lambda: _SessionContext(db_session),
    )

    result = queue_bulk_provisioning.run(
        [str(ont.id)],
        bulk_run_id=str(run.id),
    )

    assert result["bulk_run_id"] == str(run.id)
    assert result["queued"] == 1
    assert result["tasks"][0]["bulk_item_id"] == str(item.id)
    assert result["tasks"][0]["correlation_key"] == item.correlation_key
    assert enqueued[0][1]["correlation_id"] == item.correlation_key
    child_kwargs = enqueued[0][1]["kwargs"]
    assert child_kwargs["bulk_run_id"] == str(run.id)
    assert child_kwargs["bulk_item_id"] == str(item.id)
    assert child_kwargs["correlation_key"] == item.correlation_key
