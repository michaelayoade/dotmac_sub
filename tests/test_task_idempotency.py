"""Tests for task idempotency decorator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.models.task_execution import TaskExecution, TaskExecutionStatus
from app.services.task_idempotency import (
    TaskAlreadyRunning,
    TaskAlreadySucceeded,
    _build_idempotency_key,
    cleanup_old_executions,
    idempotent_task,
)


class TestBuildIdempotencyKey:
    """Tests for idempotency key building."""

    def test_key_func(self):
        """Should use key_func when provided."""

        def key_func(x, y):
            return f"{x}:{y}"

        key = _build_idempotency_key(
            "test_task",
            key_func=key_func,
            key_params=None,
            args=("a", "b"),
            kwargs={},
        )
        assert key == "test_task:a:b"

    def test_key_params(self):
        """Should build key from specified params."""
        key = _build_idempotency_key(
            "test_task",
            key_func=None,
            key_params=["param1", "param2"],
            args=(),
            kwargs={"param1": "value1", "param2": "value2", "param3": "ignored"},
        )
        assert key == "test_task:param1=value1:param2=value2"

    def test_default_hash(self):
        """Should hash args when no key_func or key_params."""
        key1 = _build_idempotency_key(
            "test_task",
            key_func=None,
            key_params=None,
            args=("a", "b"),
            kwargs={"c": "d"},
        )
        key2 = _build_idempotency_key(
            "test_task",
            key_func=None,
            key_params=None,
            args=("a", "b"),
            kwargs={"c": "d"},
        )
        # Same args should produce same key
        assert key1 == key2
        assert key1.startswith("test_task:")


class TestIdempotentTaskDecorator:
    """Tests for @idempotent_task decorator."""

    def test_first_execution_runs(self, db_session):
        """First execution should run the task."""
        call_count = 0

        @idempotent_task(key_func=lambda x: f"test:{x}")
        def test_task(x):
            nonlocal call_count
            call_count += 1
            return {"value": x}

        # Mock SessionLocal to return our test session
        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            # Patch current_task to simulate Celery context
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-1"
            with patch("app.services.task_idempotency.current_task", mock_task):
                result = test_task("arg1")

        assert call_count == 1
        assert result == {"value": "arg1"}

    def test_skip_if_already_succeeded(self, db_session):
        """Should return cached result if task already succeeded."""
        # Create a succeeded execution
        execution = TaskExecution(
            idempotency_key="test_task:test:skip",
            task_name="test_task",
            status=TaskExecutionStatus.succeeded,
            result={"cached": "result"},
        )
        db_session.add(execution)
        db_session.commit()

        call_count = 0

        @idempotent_task(key_func=lambda x: f"test:{x}")
        def test_task(x):
            nonlocal call_count
            call_count += 1
            return {"value": x}

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-2"
            with patch("app.services.task_idempotency.current_task", mock_task):
                result = test_task("skip")

        # Should not have called the actual function
        assert call_count == 0
        # Should return cached result
        assert result == {"cached": "result"}

    def test_skip_if_already_running(self, db_session):
        """Should skip if task is already running."""
        # Create a running execution (recent)
        execution = TaskExecution(
            idempotency_key="test_task:test:running",
            task_name="test_task",
            status=TaskExecutionStatus.running,
            celery_task_id="existing-task-id",
            created_at=datetime.now(UTC),
        )
        db_session.add(execution)
        db_session.commit()

        call_count = 0

        @idempotent_task(key_func=lambda x: f"test:{x}", skip_if_running=True)
        def test_task(x):
            nonlocal call_count
            call_count += 1
            return {"value": x}

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-3"
            with patch("app.services.task_idempotency.current_task", mock_task):
                result = test_task("running")

        assert call_count == 0
        assert result["skipped"] is True
        assert result["reason"] == "already_running"

    def test_raise_if_already_running(self, db_session):
        """Should raise TaskAlreadyRunning if configured."""
        execution = TaskExecution(
            idempotency_key="test_task:test:raise",
            task_name="test_task",
            status=TaskExecutionStatus.running,
            created_at=datetime.now(UTC),
        )
        db_session.add(execution)
        db_session.commit()

        @idempotent_task(key_func=lambda x: f"test:{x}", skip_if_running=False)
        def test_task(x):
            return {"value": x}

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-4"
            with patch("app.services.task_idempotency.current_task", mock_task):
                with pytest.raises(TaskAlreadyRunning):
                    test_task("raise")

    def test_raise_if_already_succeeded_when_configured(self, db_session):
        """Should raise TaskAlreadySucceeded if configured."""
        execution = TaskExecution(
            idempotency_key="test_task:test:raise_succ",
            task_name="test_task",
            status=TaskExecutionStatus.succeeded,
            result={"cached": "result"},
        )
        db_session.add(execution)
        db_session.commit()

        @idempotent_task(
            key_func=lambda x: f"test:{x}",
            return_cached_result=False,
        )
        def test_task(x):
            return {"value": x}

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-5"
            with patch("app.services.task_idempotency.current_task", mock_task):
                with pytest.raises(TaskAlreadySucceeded):
                    test_task("raise_succ")

    def test_stale_running_task_retries(self, db_session):
        """Should allow retry if running task is stale."""
        # Create a stale running execution
        stale_time = datetime.now(UTC) - timedelta(hours=2)
        execution = TaskExecution(
            idempotency_key="test_task:test:stale",
            task_name="test_task",
            status=TaskExecutionStatus.running,
            created_at=stale_time,
        )
        db_session.add(execution)
        db_session.commit()
        execution_id = execution.id

        call_count = 0

        @idempotent_task(
            key_func=lambda x: f"test:{x}",
            stale_timeout=timedelta(hours=1),
        )
        def test_task(x):
            nonlocal call_count
            call_count += 1
            return {"value": x}

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-6"
            with patch("app.services.task_idempotency.current_task", mock_task):
                result = test_task("stale")

        # Stale task should be marked failed and new execution should run
        assert call_count == 1
        assert result == {"value": "stale"}

        # Check original execution was marked as failed (re-fetch from db)
        stale_execution = db_session.get(TaskExecution, execution_id)
        assert stale_execution is not None
        assert stale_execution.status == TaskExecutionStatus.failed

    def test_exception_marks_as_failed(self, db_session):
        """Should mark execution as failed on exception."""
        unique_key = f"exception:{uuid.uuid4().hex}"

        @idempotent_task(key_func=lambda x: x)
        def test_task(x):
            raise ValueError("Test error")

        with patch(
            "app.services.task_idempotency.SessionLocal", return_value=db_session
        ):
            mock_task = MagicMock()
            mock_task.name = "test_task"
            mock_task.request.id = "celery-task-7"
            with patch("app.services.task_idempotency.current_task", mock_task):
                with pytest.raises(ValueError):
                    test_task(unique_key)

        # Check execution was marked as failed
        from sqlalchemy import select

        stmt = select(TaskExecution).where(
            TaskExecution.idempotency_key == f"test_task:{unique_key}"
        )
        execution = db_session.scalars(stmt).first()
        assert execution is not None
        assert execution.status == TaskExecutionStatus.failed
        assert "Test error" in (execution.error_message or "")


class TestCleanupOldExecutions:
    """Tests for cleanup_old_executions function."""

    def test_cleanup_old_completed_executions(self, db_session):
        """Should remove old completed executions."""
        # Create old succeeded execution
        old_time = datetime.now(UTC) - timedelta(days=60)
        old_execution = TaskExecution(
            idempotency_key=f"test:old:{uuid.uuid4().hex}",
            task_name="test_task",
            status=TaskExecutionStatus.succeeded,
            created_at=old_time,
        )
        db_session.add(old_execution)

        # Create recent succeeded execution
        recent_execution = TaskExecution(
            idempotency_key=f"test:recent:{uuid.uuid4().hex}",
            task_name="test_task",
            status=TaskExecutionStatus.succeeded,
            created_at=datetime.now(UTC) - timedelta(days=5),
        )
        db_session.add(recent_execution)
        db_session.commit()

        old_id = old_execution.id
        recent_id = recent_execution.id

        # Run cleanup
        deleted = cleanup_old_executions(db_session, max_age_days=30)

        # Old one should be deleted
        assert deleted >= 1
        assert db_session.get(TaskExecution, old_id) is None
        # Recent one should remain
        assert db_session.get(TaskExecution, recent_id) is not None

    def test_does_not_cleanup_running_tasks(self, db_session):
        """Should not remove running tasks even if old."""
        old_time = datetime.now(UTC) - timedelta(days=60)
        running_execution = TaskExecution(
            idempotency_key=f"test:running:{uuid.uuid4().hex}",
            task_name="test_task",
            status=TaskExecutionStatus.running,
            created_at=old_time,
        )
        db_session.add(running_execution)
        db_session.commit()

        execution_id = running_execution.id

        cleanup_old_executions(db_session, max_age_days=30)

        # Running task should not be deleted
        assert db_session.get(TaskExecution, execution_id) is not None
