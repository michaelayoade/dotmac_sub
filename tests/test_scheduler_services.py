"""Tests for scheduler service."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.models.scheduler import ScheduledTask, ScheduleType
from app.schemas.scheduler import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services import scheduler as scheduler_service
from app.services.common import apply_ordering, apply_pagination, validate_enum


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestApplyOrdering:
    """Tests for _apply_ordering function."""

    def test_valid_order_by_asc(self, db_session):
        """Test valid order_by with asc direction."""
        query = db_session.query(ScheduledTask)
        allowed = {"name": ScheduledTask.name, "created_at": ScheduledTask.created_at}
        result = apply_ordering(query, "name", "asc", allowed)
        # Should not raise
        assert result is not None

    def test_valid_order_by_desc(self, db_session):
        """Test valid order_by with desc direction."""
        query = db_session.query(ScheduledTask)
        allowed = {"name": ScheduledTask.name, "created_at": ScheduledTask.created_at}
        result = apply_ordering(query, "name", "desc", allowed)
        assert result is not None

    def test_invalid_order_by(self, db_session):
        """Test invalid order_by raises HTTPException."""
        query = db_session.query(ScheduledTask)
        allowed = {"name": ScheduledTask.name}

        with pytest.raises(HTTPException) as exc_info:
            apply_ordering(query, "invalid_column", "asc", allowed)

        assert exc_info.value.status_code == 400
        assert "Invalid order_by" in exc_info.value.detail


class TestApplyPagination:
    """Tests for _apply_pagination function."""

    def test_applies_limit_and_offset(self, db_session):
        """Test applies limit and offset to query."""
        query = db_session.query(ScheduledTask)
        result = apply_pagination(query, 10, 5)
        # Should not raise
        assert result is not None


class TestValidateScheduleType:
    """Tests for _validate_schedule_type function."""

    def test_returns_none_for_none(self):
        """Test returns None for None input."""
        result = scheduler_service._validate_schedule_type(None)
        assert result is None

    def test_returns_enum_if_already_enum(self):
        """Test returns enum if already ScheduleType."""
        result = scheduler_service._validate_schedule_type(ScheduleType.interval)
        assert result == ScheduleType.interval

    def test_converts_valid_string(self):
        """Test converts valid string to enum."""
        result = scheduler_service._validate_schedule_type("interval")
        assert result == ScheduleType.interval

    def test_invalid_string_raises(self):
        """Test invalid string raises HTTPException."""
        with pytest.raises(HTTPException) as exc_info:
            scheduler_service._validate_schedule_type("invalid_type")

        assert exc_info.value.status_code == 400
        assert "Invalid schedule_type" in exc_info.value.detail


# =============================================================================
# ScheduledTasks CRUD Tests
# =============================================================================


class TestScheduledTasksCreate:
    """Tests for ScheduledTasks.create."""

    def test_creates_task(self, db_session):
        """Test creates a scheduled task."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="test-task",
                task_name="app.tasks.test",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        assert task.name == "test-task"
        assert task.task_name == "app.tasks.test"
        assert task.schedule_type == ScheduleType.interval
        assert task.interval_seconds == 60

    def test_raises_for_invalid_interval(self, db_session):
        """Test raises for interval < 1."""
        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.create(
                db_session,
                ScheduledTaskCreate(
                    name="bad-task",
                    task_name="app.tasks.bad",
                    schedule_type=ScheduleType.interval,
                    interval_seconds=0,
                ),
            )

        assert exc_info.value.status_code == 400
        assert "interval_seconds must be >= 1" in exc_info.value.detail


class TestScheduledTasksGet:
    """Tests for ScheduledTasks.get."""

    def test_gets_task_by_id(self, db_session):
        """Test gets task by ID."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="get-test",
                task_name="app.tasks.get",
                schedule_type=ScheduleType.interval,
                interval_seconds=30,
            ),
        )
        fetched = scheduler_service.scheduled_tasks.get(db_session, str(task.id))
        assert fetched.id == task.id

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent task."""
        import uuid

        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.get(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestScheduledTasksList:
    """Tests for ScheduledTasks.list."""

    def test_lists_all_tasks(self, db_session):
        """Test lists all tasks."""
        scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="list-test-1",
                task_name="app.tasks.list1",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="list-test-2",
                task_name="app.tasks.list2",
                schedule_type=ScheduleType.interval,
                interval_seconds=120,
            ),
        )

        tasks = scheduler_service.scheduled_tasks.list(
            db_session,
            enabled=None,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert len(tasks) >= 2

    def test_filters_by_enabled(self, db_session):
        """Test filters by enabled status."""
        scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="enabled-task",
                task_name="app.tasks.enabled",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
                enabled=True,
            ),
        )
        scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="disabled-task",
                task_name="app.tasks.disabled",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
                enabled=False,
            ),
        )

        enabled_tasks = scheduler_service.scheduled_tasks.list(
            db_session,
            enabled=True,
            order_by="created_at",
            order_dir="asc",
            limit=100,
            offset=0,
        )
        assert all(t.enabled for t in enabled_tasks)


class TestScheduledTasksUpdate:
    """Tests for ScheduledTasks.update."""

    def test_updates_task(self, db_session):
        """Test updates task fields."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="update-test",
                task_name="app.tasks.update",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        updated = scheduler_service.scheduled_tasks.update(
            db_session,
            str(task.id),
            ScheduledTaskUpdate(name="updated-name", interval_seconds=120),
        )
        assert updated.name == "updated-name"
        assert updated.interval_seconds == 120

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent task."""
        import uuid

        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.update(
                db_session, str(uuid.uuid4()), ScheduledTaskUpdate(name="new")
            )

        assert exc_info.value.status_code == 404

    def test_validates_schedule_type_on_update(self, db_session):
        """Test validates schedule_type on update."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="type-test",
                task_name="app.tasks.type",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        updated = scheduler_service.scheduled_tasks.update(
            db_session,
            str(task.id),
            ScheduledTaskUpdate(schedule_type="interval"),
        )
        assert updated.schedule_type == ScheduleType.interval

    def test_raises_for_invalid_interval_on_update(self, db_session):
        """Test raises for interval < 1 on update."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="interval-test",
                task_name="app.tasks.interval",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.update(
                db_session, str(task.id), ScheduledTaskUpdate(interval_seconds=0)
            )

        assert exc_info.value.status_code == 400
        assert "interval_seconds must be >= 1" in exc_info.value.detail


class TestScheduledTasksDelete:
    """Tests for ScheduledTasks.delete."""

    def test_deletes_task(self, db_session):
        """Test deletes task."""
        task = scheduler_service.scheduled_tasks.create(
            db_session,
            ScheduledTaskCreate(
                name="delete-test",
                task_name="app.tasks.delete",
                schedule_type=ScheduleType.interval,
                interval_seconds=60,
            ),
        )
        task_id = str(task.id)
        scheduler_service.scheduled_tasks.delete(db_session, task_id)

        # Verify deleted
        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.get(db_session, task_id)
        assert exc_info.value.status_code == 404

    def test_raises_for_not_found(self, db_session):
        """Test raises 404 for non-existent task."""
        import uuid

        with pytest.raises(HTTPException) as exc_info:
            scheduler_service.scheduled_tasks.delete(db_session, str(uuid.uuid4()))

        assert exc_info.value.status_code == 404


# =============================================================================
# Utility Function Tests
# =============================================================================


class TestRefreshSchedule:
    """Tests for refresh_schedule function."""

    def test_returns_message(self):
        """Test returns informational message."""
        result = scheduler_service.refresh_schedule()
        assert "detail" in result
        assert "Celery" in result["detail"]


class TestEnqueueTask:
    """Tests for enqueue_task function."""

    def test_enqueues_task(self):
        """Test enqueues task to Celery."""
        mock_result = MagicMock()
        mock_result.id = "task-123"

        with patch(
            "app.celery_app.celery_app.send_task", return_value=mock_result
        ) as mock_send:
            result = scheduler_service.enqueue_task(
                "app.tasks.test", ["arg1"], {"key": "value"}
            )

            mock_send.assert_called_once_with(
                "app.tasks.test", args=["arg1"], kwargs={"key": "value"}
            )
            assert result["queued"] is True
            assert result["task_id"] == "task-123"

    def test_enqueues_with_empty_args(self):
        """Test enqueues task with empty args and kwargs."""
        mock_result = MagicMock()
        mock_result.id = "task-456"

        with patch(
            "app.celery_app.celery_app.send_task", return_value=mock_result
        ) as mock_send:
            result = scheduler_service.enqueue_task("app.tasks.empty", None, None)

            mock_send.assert_called_once_with(
                "app.tasks.empty", args=[], kwargs={}
            )
            assert result["queued"] is True
