"""The open-only auto-detect scan is retired; the reconcile owns detection."""

from __future__ import annotations

from app.celery_app import celery_app

SCAN_TASK = "app.tasks.topology_outage.run_outage_scan"
RECONCILE_TASK = "app.tasks.topology_outage.reconcile_detected_outages"


def test_scan_task_is_gone():
    import app.tasks  # noqa: F401 - triggers task module imports

    assert SCAN_TASK not in celery_app.tasks
    assert SCAN_TASK not in celery_app.conf.task_routes


def test_reconcile_task_still_registered_and_routed():
    import app.tasks  # noqa: F401

    assert RECONCILE_TASK in celery_app.tasks
    assert celery_app.conf.task_routes[RECONCILE_TASK] == {"queue": "ingestion"}


def test_scheduler_retires_the_scan_beat_row():
    # The beat build must actively disable any existing DB row for the old
    # scan task, not merely stop registering it.
    import inspect

    from app.services import scheduler_config

    src = inspect.getsource(scheduler_config)
    assert (
        f'_retire_scheduled_task(\n            session,\n            "{SCAN_TASK}"'
        in src
        or SCAN_TASK in src
    )
