"""Topology reconcile task is registered + routed (Phase 1, Task 5)."""

from __future__ import annotations

from app.celery_app import celery_app

TASK_NAME = "app.tasks.topology_sync.run_topology_reconcile"


def test_task_is_registered():
    import app.tasks  # noqa: F401 - triggers task module imports

    assert TASK_NAME in celery_app.tasks


def test_task_routed_to_ingestion_queue():
    assert celery_app.conf.task_routes[TASK_NAME] == {"queue": "ingestion"}


def test_task_exported_from_package():
    import app.tasks as tasks

    assert "run_topology_reconcile" in tasks.__all__
    assert hasattr(tasks, "run_topology_reconcile")
