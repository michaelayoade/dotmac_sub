"""The account-access control loop is mandatory and observable."""

from __future__ import annotations

from tests.test_scheduled_tasks_registered import _declared_scheduled_task_names

TASK = "app.tasks.radius.run_enforcement_reconciler"


def test_enforcement_reconciler_is_declared_in_scheduler() -> None:
    assert TASK in _declared_scheduled_task_names()


def test_enforcement_reconciler_is_a_registered_task() -> None:
    from app.celery_app import celery_app

    assert TASK in celery_app.tasks


def test_enforcement_reconciler_has_reliability_contract() -> None:
    from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS

    assert TASK in TASK_RELIABILITY_CONTRACTS


def test_enforcement_reconciler_is_permanent() -> None:
    from app.services.scheduler import PERMANENT_CUSTOMER_LIFECYCLE_TASKS

    assert TASK in PERMANENT_CUSTOMER_LIFECYCLE_TASKS
