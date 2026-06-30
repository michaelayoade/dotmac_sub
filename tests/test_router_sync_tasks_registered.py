"""Regression: router_sync tasks must be registered with the Celery worker.

The keystone ``router_sync.capture_scheduled_snapshots`` is referenced by the
scheduler (``scheduler_config``) and run by celery-beat. It lives in
``app/tasks/router_sync.py`` but is only registered if ``app/tasks/__init__.py``
imports it — importing app.tasks is what the worker does at startup. A missing
import means beat dispatches an *unregistered* task and the capture never runs
(router_config_snapshots stays empty). Guard against that here.
"""

from __future__ import annotations

import app.tasks  # noqa: F401  (import side effect: registers task modules)
from app.celery_app import celery_app

_EXPECTED = {
    "router_sync.capture_scheduled_snapshots",
    "router_sync.sync_all_system_info",
    "router_sync.sync_all_interfaces",
    "router_sync.cleanup_idle_tunnels",
    "router_sync.execute_config_push",
}


def test_router_sync_tasks_are_registered():
    missing = _EXPECTED - set(celery_app.tasks.keys())
    assert not missing, (
        f"router_sync tasks not registered with Celery: {sorted(missing)}"
    )
