"""Regression: the enforcement reconciler stays wired into the beat schedule.

run_enforcement_reconciler (SP-1) closes the gap where a billing suspension
only rejects at the next re-auth while a live PPPoE session survives — a
suspended subscriber stays online (revenue leak). The task existed but was
never scheduled; this pins that it remains declared in scheduler_config and
resolves to a registered Celery task.
"""

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
