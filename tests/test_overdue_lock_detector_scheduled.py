"""The money-adjacent overdue-lock repair remains detect-only."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.test_scheduled_tasks_registered import _declared_scheduled_task_names

TASK = "app.tasks.enforcement.detect_stale_overdue_locks"


def test_overdue_detector_is_registered() -> None:
    from app.celery_app import celery_app
    from app.services.task_reliability import TASK_RELIABILITY_CONTRACTS

    assert TASK in _declared_scheduled_task_names()
    assert TASK in celery_app.tasks
    assert TASK in TASK_RELIABILITY_CONTRACTS


def test_overdue_lock_task_is_dry_run_and_writes_nothing() -> None:
    from app.services import enforcement_scheduled
    from app.tasks import enforcement

    fake_db = MagicMock()
    reconcile_result = MagicMock(
        candidates=2, restored=0, lock_cleared_only=0, skipped=0
    )
    with (
        patch.object(enforcement_scheduled, "SessionLocal", return_value=fake_db),
        patch(
            "app.services.stale_overdue_lock_reconcile.reconcile",
            return_value=reconcile_result,
        ) as reconcile,
    ):
        result = enforcement.detect_stale_overdue_locks()

    assert reconcile.call_args.kwargs["apply"] is False
    fake_db.commit.assert_not_called()
    assert result == {"candidates": 2, "applied": 0}
