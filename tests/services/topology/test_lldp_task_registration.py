"""LLDP poll task is registered + routed (Phase 2, P2.5)."""

from __future__ import annotations

from app.celery_app import celery_app

TASK = "app.tasks.topology_lldp.run_lldp_topology_poll"


def test_task_registered():
    import app.tasks  # noqa: F401

    assert TASK in celery_app.tasks


def test_task_routed_to_ingestion():
    assert celery_app.conf.task_routes[TASK] == {"queue": "ingestion"}


def test_task_exported():
    import app.tasks as tasks

    assert "run_lldp_topology_poll" in tasks.__all__
    assert hasattr(tasks, "run_lldp_topology_poll")
