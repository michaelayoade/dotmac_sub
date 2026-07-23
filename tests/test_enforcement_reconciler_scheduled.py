"""The account-access control loop is mandatory and observable."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from tests.test_scheduled_tasks_registered import _declared_scheduled_task_names

TASK = "app.tasks.radius.run_enforcement_reconciler"
RADIUS_REFRESH_TASK = "app.tasks.radius_population.refresh_radius_from_subs"


def test_enforcement_reconciler_is_declared_in_scheduler() -> None:
    assert TASK in _declared_scheduled_task_names()


def test_enforcement_reconciler_is_a_registered_task() -> None:
    from app.celery_app import celery_app

    assert TASK in celery_app.tasks


def test_enforcement_reconciler_has_reliability_contract() -> None:
    from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS

    assert TASK in TASK_RELIABILITY_CONTRACTS


def test_enforcement_reconciler_is_permanent() -> None:
    from app.services.scheduler import PERMANENT_LIFECYCLE_TASKS

    assert TASK in PERMANENT_LIFECYCLE_TASKS


def test_radius_refresh_transport_is_not_scheduled_independently() -> None:
    assert RADIUS_REFRESH_TASK not in _declared_scheduled_task_names()


def test_enforcement_reconciler_is_single_flight() -> None:
    from app.tasks.radius import run_enforcement_reconciler

    @contextmanager
    def _already_running(*args, **kwargs):
        yield False

    with (
        patch(
            "app.tasks.radius.postgres_session_advisory_lock",
            _already_running,
        ),
        patch("app.tasks.radius._run_enforcement_reconciler") as run,
        patch("app.services.task_heartbeat.record_skip") as record_skip,
    ):
        result = run_enforcement_reconciler.run()

    assert result == {"skipped_already_running": 1}
    run.assert_not_called()
    record_skip.assert_called_once_with(TASK)
