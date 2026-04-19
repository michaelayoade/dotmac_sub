"""Tests for bulk ONT saga dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def test_bulk_provision_action_queues_saga_orchestrator(monkeypatch):
    import app.celery_app as celery_module
    from app.tasks.ont_bulk import execute_bulk_action

    enqueued: list[tuple[Any, dict[str, Any]]] = []

    def fake_enqueue(task_or_name, **kwargs):  # type: ignore[no-untyped-def]
        enqueued.append((task_or_name, kwargs))
        return SimpleNamespace(id="bulk-orchestrator-task")

    monkeypatch.setattr(celery_module, "enqueue_celery_task", fake_enqueue)

    result = execute_bulk_action.run(
        ["ont-a", "ont-b"],
        "provision_saga",
        {
            "profile_id": "profile-1",
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
        "orchestrator_task_id": "bulk-orchestrator-task",
        "saga_name": "full_provisioning",
    }
    assert len(enqueued) == 1
    task_name, enqueue_kwargs = enqueued[0]
    assert task_name == "app.tasks.saga.queue_bulk_saga_executions"
    assert enqueue_kwargs["correlation_id"] == "ont_bulk_saga:full_provisioning:2"
    assert enqueue_kwargs["source"] == "ont_bulk_action"
    task_kwargs = enqueue_kwargs["kwargs"]
    assert task_kwargs["ont_ids"] == ["ont-a", "ont-b"]
    assert task_kwargs["step_data"] == {
        "profile_id": "profile-1",
        "tr069_olt_profile_id": "tr069-profile-1",
    }
    assert task_kwargs["max_parallel"] == 10
    assert task_kwargs["chunk_delay_seconds"] == 20


def test_bulk_saga_orchestrator_dedupes_and_chunks(monkeypatch):
    import app.celery_app as celery_module
    from app.services.network.ont_provisioning import saga as saga_module
    from app.tasks.saga import queue_bulk_saga_executions

    enqueued: list[tuple[Any, dict[str, Any]]] = []

    monkeypatch.setattr(
        saga_module,
        "get_saga_by_name",
        lambda saga_name: object() if saga_name == "full_provisioning" else None,
    )

    def fake_enqueue(task_or_name, **kwargs):  # type: ignore[no-untyped-def]
        enqueued.append((task_or_name, kwargs))
        return SimpleNamespace(id=f"saga-task-{len(enqueued)}")

    monkeypatch.setattr(celery_module, "enqueue_celery_task", fake_enqueue)

    result = queue_bulk_saga_executions.run(
        "full_provisioning",
        ["ont-1", "ont-2", "ont-1", "", "ont-3"],
        step_data={"profile_id": "profile-1"},
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
        getattr(task_or_name, "name", None) == "app.tasks.saga.execute_saga"
        for task_or_name, _ in enqueued
    )
    assert all(
        kwargs["kwargs"]["step_data"] == {"profile_id": "profile-1"}
        for _, kwargs in enqueued
    )


def test_bulk_saga_orchestrator_rejects_unknown_saga(monkeypatch):
    from app.services.network.ont_provisioning import saga as saga_module
    from app.tasks.saga import queue_bulk_saga_executions

    monkeypatch.setattr(saga_module, "get_saga_by_name", lambda saga_name: None)

    result = queue_bulk_saga_executions.run(
        "missing_saga",
        ["ont-1"],
    )

    assert result == {
        "queued": 0,
        "errors": 1,
        "skipped": 1,
        "message": "Saga not found: missing_saga",
        "tasks": [],
    }
