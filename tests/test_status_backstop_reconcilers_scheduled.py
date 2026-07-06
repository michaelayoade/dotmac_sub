"""Regression tests for the SP-8 status-backstop reconciler tasks.

Two dormant reconcilers are now scheduled as the catch-up for lost enforcement
events:

* ``reconcile_account_status_drift`` — APPLY mode (unblocks subscribers whose
  subs are all active; pure service-state, all-active filter is the guard).
* ``detect_stale_overdue_locks`` — DRY-RUN detector (money-adjacent; surfaces
  candidates for manual review, writes nothing).

The mode split is the safety contract, so these pin it explicitly, plus the
usual scheduled/registered/contracted guards.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.test_scheduled_tasks_registered import _declared_scheduled_task_names

ACCOUNT_STATUS = "app.tasks.enforcement.reconcile_account_status_drift"
OVERDUE_DETECT = "app.tasks.enforcement.detect_stale_overdue_locks"


def test_both_reconcilers_declared_registered_and_contracted() -> None:
    from app.celery_app import celery_app
    from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS

    declared = _declared_scheduled_task_names()
    for task in (ACCOUNT_STATUS, OVERDUE_DETECT):
        assert task in declared
        assert task in celery_app.tasks
        assert task in TASK_RELIABILITY_CONTRACTS


def test_account_status_task_runs_in_apply_mode_and_commits() -> None:
    from app.tasks import enforcement

    fake_db = MagicMock()
    summary = MagicMock(candidates=1, changed=1, errors=0)
    with (
        patch.object(enforcement, "SessionLocal", return_value=fake_db),
        patch(
            "app.services.account_status_reconcile.reconcile_cohort",
            return_value=summary,
        ) as reconcile_cohort,
    ):
        result = enforcement.reconcile_account_status_drift()

    # APPLY mode: dry_run=False, and it commits the repair.
    assert reconcile_cohort.call_args.kwargs["dry_run"] is False
    fake_db.commit.assert_called_once()
    assert result["changed"] == 1


def test_overdue_lock_task_is_dry_run_and_writes_nothing() -> None:
    from app.tasks import enforcement

    fake_db = MagicMock()
    reconcile_result = MagicMock(
        candidates=2, restored=0, lock_cleared_only=0, skipped=0
    )
    with (
        patch.object(enforcement, "SessionLocal", return_value=fake_db),
        patch(
            "app.services.stale_overdue_lock_reconcile.reconcile",
            return_value=reconcile_result,
        ) as reconcile,
    ):
        result = enforcement.detect_stale_overdue_locks()

    # DRY-RUN: apply=False, and the task never commits.
    assert reconcile.call_args.kwargs["apply"] is False
    fake_db.commit.assert_not_called()
    assert result == {"candidates": 2, "applied": 0}
