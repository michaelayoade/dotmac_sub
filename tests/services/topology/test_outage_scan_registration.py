"""Outage auto-detect scan task is registered + routed + scheduled (Phase 5b)."""

from __future__ import annotations

from pathlib import Path

from app.celery_app import celery_app

TASK = "app.tasks.topology_outage.run_outage_scan"


def test_task_registered():
    import app.tasks  # noqa: F401

    assert TASK in celery_app.tasks


def test_task_routed_to_ingestion():
    assert celery_app.conf.task_routes[TASK] == {"queue": "ingestion"}


def test_task_exported():
    import app.tasks as tasks

    assert "run_outage_scan" in tasks.__all__
    assert hasattr(tasks, "run_outage_scan")


def test_beat_row_registered_with_interval_floor():
    """The scheduler_config beat row exists: name topology_outage_scan, driven
    by the outage_scan_interval_seconds setting with a 120s floor."""
    import app.services.scheduler_config as scheduler_config

    source = Path(scheduler_config.__file__).read_text()
    assert 'name="topology_outage_scan"' in source
    assert f'task_name="{TASK}"' in source
    assert '"outage_scan_interval_seconds"' in source
    assert "max(outage_scan_seconds, 120)" in source
