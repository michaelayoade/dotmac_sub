"""Tests for scheduler config services."""

import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.models.scheduler import ScheduleType, ScheduledTask
from app.services import scheduler_config


# =============================================================================
# Environment Variable Helper Tests
# =============================================================================


class TestEnvValue:
    """Tests for _env_value helper."""

    def test_returns_value_when_set(self, monkeypatch):
        """Test returns value when env var is set."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        result = scheduler_config._env_value("TEST_VAR")
        assert result == "test_value"

    def test_returns_none_when_not_set(self):
        """Test returns None when env var not set."""
        # Make sure it's not set
        os.environ.pop("NONEXISTENT_VAR", None)
        result = scheduler_config._env_value("NONEXISTENT_VAR")
        assert result is None

    def test_returns_none_for_empty_string(self, monkeypatch):
        """Test returns None for empty string."""
        monkeypatch.setenv("EMPTY_VAR", "")
        result = scheduler_config._env_value("EMPTY_VAR")
        assert result is None


class TestEnvBool:
    """Tests for _env_bool helper."""

    def test_returns_true_for_true_values(self, monkeypatch):
        """Test returns True for various true values."""
        for value in ["1", "true", "True", "TRUE", "yes", "Yes", "on", "ON"]:
            monkeypatch.setenv("BOOL_VAR", value)
            result = scheduler_config._env_bool("BOOL_VAR")
            assert result is True, f"Expected True for '{value}'"

    def test_returns_false_for_false_values(self, monkeypatch):
        """Test returns False for non-true values."""
        monkeypatch.setenv("BOOL_VAR", "false")
        result = scheduler_config._env_bool("BOOL_VAR")
        assert result is False

    def test_returns_none_when_not_set(self):
        """Test returns None when env var not set."""
        os.environ.pop("NONEXISTENT_BOOL", None)
        result = scheduler_config._env_bool("NONEXISTENT_BOOL")
        assert result is None


class TestEnvInt:
    """Tests for _env_int helper."""

    def test_returns_int_when_valid(self, monkeypatch):
        """Test returns int when value is valid."""
        monkeypatch.setenv("INT_VAR", "42")
        result = scheduler_config._env_int("INT_VAR")
        assert result == 42

    def test_returns_none_when_not_set(self):
        """Test returns None when env var not set."""
        os.environ.pop("NONEXISTENT_INT", None)
        result = scheduler_config._env_int("NONEXISTENT_INT")
        assert result is None

    def test_returns_none_for_invalid_int(self, monkeypatch):
        """Test returns None when value is not a valid int."""
        monkeypatch.setenv("INT_VAR", "not_a_number")
        result = scheduler_config._env_int("INT_VAR")
        assert result is None


# =============================================================================
# Database Setting Helper Tests
# =============================================================================


class TestGetSettingValue:
    """Tests for _get_setting_value helper."""

    def test_returns_none_when_not_found(self, db_session):
        """Test returns None when setting not found."""
        result = scheduler_config._get_setting_value(
            db_session, SettingDomain.scheduler, "nonexistent"
        )
        assert result is None

    def test_returns_value_text(self, db_session):
        """Test returns value_text when set."""
        setting = DomainSetting(
            domain=SettingDomain.scheduler,
            key="broker_url",
            value_text="redis://localhost:6379/0",
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = scheduler_config._get_setting_value(
            db_session, SettingDomain.scheduler, "broker_url"
        )
        assert result == "redis://localhost:6379/0"

    def test_returns_value_json_as_string(self, db_session):
        """Test returns value_json as string."""
        setting = DomainSetting(
            domain=SettingDomain.scheduler,
            key="json_setting",
            value_type=SettingValueType.json,
            value_text=None,
            value_json={"config": "value"},
            is_active=True,
        )
        db_session.add(setting)
        db_session.commit()

        result = scheduler_config._get_setting_value(
            db_session, SettingDomain.scheduler, "json_setting"
        )
        assert "config" in result

    def test_ignores_inactive_settings(self, db_session):
        """Test ignores inactive settings."""
        setting = DomainSetting(
            domain=SettingDomain.scheduler,
            key="inactive_key",
            value_text="inactive_value",
            is_active=False,
        )
        db_session.add(setting)
        db_session.commit()

        result = scheduler_config._get_setting_value(
            db_session, SettingDomain.scheduler, "inactive_key"
        )
        assert result is None


# =============================================================================
# Effective Value Helper Tests
# =============================================================================


class TestEffectiveBool:
    """Tests for _effective_bool helper."""

    def test_env_takes_precedence(self, db_session, monkeypatch):
        """Test env var takes precedence over db setting."""
        monkeypatch.setenv("TEST_BOOL_ENV", "true")
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_bool",
            value_text="false",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_bool(
            db_session, SettingDomain.scheduler, "test_bool", "TEST_BOOL_ENV", False
        )
        assert result is True

    def test_db_value_when_no_env(self, db_session):
        """Test uses db value when no env var."""
        os.environ.pop("TEST_BOOL_ENV2", None)
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_bool2",
            value_text="true",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_bool(
            db_session, SettingDomain.scheduler, "test_bool2", "TEST_BOOL_ENV2", False
        )
        assert result is True

    def test_default_when_neither_set(self, db_session):
        """Test uses default when neither env nor db set."""
        os.environ.pop("TEST_BOOL_ENV3", None)

        result = scheduler_config._effective_bool(
            db_session, SettingDomain.scheduler, "nonexistent", "TEST_BOOL_ENV3", True
        )
        assert result is True


class TestEffectiveInt:
    """Tests for _effective_int helper."""

    def test_env_takes_precedence(self, db_session, monkeypatch):
        """Test env var takes precedence over db setting."""
        monkeypatch.setenv("TEST_INT_ENV", "100")
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_int",
            value_text="50",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_int(
            db_session, SettingDomain.scheduler, "test_int", "TEST_INT_ENV", 25
        )
        assert result == 100

    def test_db_value_when_no_env(self, db_session):
        """Test uses db value when no env var."""
        os.environ.pop("TEST_INT_ENV2", None)
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_int2",
            value_text="75",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_int(
            db_session, SettingDomain.scheduler, "test_int2", "TEST_INT_ENV2", 25
        )
        assert result == 75

    def test_default_when_neither_set(self, db_session):
        """Test uses default when neither env nor db set."""
        os.environ.pop("TEST_INT_ENV3", None)

        result = scheduler_config._effective_int(
            db_session, SettingDomain.scheduler, "nonexistent", "TEST_INT_ENV3", 42
        )
        assert result == 42

    def test_default_for_invalid_db_value(self, db_session):
        """Test uses default when db value is invalid."""
        os.environ.pop("TEST_INT_ENV4", None)
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="invalid_int",
            value_text="not_a_number",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_int(
            db_session, SettingDomain.scheduler, "invalid_int", "TEST_INT_ENV4", 99
        )
        assert result == 99


class TestEffectiveStr:
    """Tests for _effective_str helper."""

    def test_env_takes_precedence(self, db_session, monkeypatch):
        """Test env var takes precedence over db setting."""
        monkeypatch.setenv("TEST_STR_ENV", "env_value")
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_str",
            value_text="db_value",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_str(
            db_session, SettingDomain.scheduler, "test_str", "TEST_STR_ENV", "default"
        )
        assert result == "env_value"

    def test_db_value_when_no_env(self, db_session):
        """Test uses db value when no env var."""
        os.environ.pop("TEST_STR_ENV2", None)
        db_session.add(DomainSetting(
            domain=SettingDomain.scheduler,
            key="test_str2",
            value_text="db_value",
            is_active=True,
        ))
        db_session.commit()

        result = scheduler_config._effective_str(
            db_session, SettingDomain.scheduler, "test_str2", "TEST_STR_ENV2", "default"
        )
        assert result == "db_value"

    def test_default_when_neither_set(self, db_session):
        """Test uses default when neither env nor db set."""
        os.environ.pop("TEST_STR_ENV3", None)

        result = scheduler_config._effective_str(
            db_session, SettingDomain.scheduler, "nonexistent", "TEST_STR_ENV3", "fallback"
        )
        assert result == "fallback"


# =============================================================================
# Sync Scheduled Task Tests
# =============================================================================


class TestSyncScheduledTask:
    """Tests for _sync_scheduled_task helper."""

    def test_creates_task_when_enabled(self, db_session):
        """Test creates new task when enabled and doesn't exist."""
        scheduler_config._sync_scheduled_task(
            db_session,
            name="test_task",
            task_name="app.tasks.test.run_test",
            enabled=True,
            interval_seconds=3600,
        )

        task = db_session.query(ScheduledTask).filter(
            ScheduledTask.task_name == "app.tasks.test.run_test"
        ).first()

        assert task is not None
        assert task.name == "test_task"
        assert task.interval_seconds == 3600
        assert task.enabled is True

    def test_does_not_create_when_disabled(self, db_session):
        """Test does not create task when disabled and doesn't exist."""
        scheduler_config._sync_scheduled_task(
            db_session,
            name="disabled_task",
            task_name="app.tasks.disabled.run",
            enabled=False,
            interval_seconds=3600,
        )

        task = db_session.query(ScheduledTask).filter(
            ScheduledTask.task_name == "app.tasks.disabled.run"
        ).first()

        assert task is None

    def test_updates_existing_task_name(self, db_session):
        """Test updates name of existing task."""
        task = ScheduledTask(
            name="original_name",
            task_name="app.tasks.update.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._sync_scheduled_task(
            db_session,
            name="updated_name",
            task_name="app.tasks.update.run",
            enabled=True,
            interval_seconds=3600,
        )

        db_session.refresh(task)
        assert task.name == "updated_name"

    def test_updates_existing_task_interval(self, db_session):
        """Test updates interval of existing task."""
        task = ScheduledTask(
            name="interval_task",
            task_name="app.tasks.interval.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._sync_scheduled_task(
            db_session,
            name="interval_task",
            task_name="app.tasks.interval.run",
            enabled=True,
            interval_seconds=7200,
        )

        db_session.refresh(task)
        assert task.interval_seconds == 7200

    def test_updates_existing_task_enabled(self, db_session):
        """Test updates enabled status of existing task."""
        task = ScheduledTask(
            name="enable_task",
            task_name="app.tasks.enable.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._sync_scheduled_task(
            db_session,
            name="enable_task",
            task_name="app.tasks.enable.run",
            enabled=False,
            interval_seconds=3600,
        )

        db_session.refresh(task)
        assert task.enabled is False

    def test_no_update_when_unchanged(self, db_session):
        """Test no commit when nothing changed."""
        task = ScheduledTask(
            name="unchanged_task",
            task_name="app.tasks.unchanged.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        original_updated_at = task.updated_at

        scheduler_config._sync_scheduled_task(
            db_session,
            name="unchanged_task",
            task_name="app.tasks.unchanged.run",
            enabled=True,
            interval_seconds=3600,
        )

        db_session.refresh(task)
        # updated_at should be same since nothing changed
        assert task.updated_at == original_updated_at


# =============================================================================
# Get Celery Config Tests
# =============================================================================


class TestGetCeleryConfig:
    """Tests for get_celery_config function."""

    def test_returns_default_config(self, monkeypatch):
        """Test returns default config when nothing configured."""
        # Clear env vars
        for var in ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND",
                    "CELERY_TIMEZONE", "REDIS_URL"]:
            monkeypatch.delenv(var, raising=False)

        # Mock SessionLocal to return a mock session
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://localhost:6379/0"
        assert config["result_backend"] == "redis://localhost:6379/1"
        assert config["timezone"] == "UTC"
        assert config["beat_max_loop_interval"] == 5
        assert config["beat_refresh_seconds"] == 30

    def test_uses_env_vars(self, monkeypatch):
        """Test uses environment variables."""
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://backend:6379/1")
        monkeypatch.setenv("CELERY_TIMEZONE", "America/New_York")
        monkeypatch.setenv("CELERY_BEAT_MAX_LOOP_INTERVAL", "10")
        monkeypatch.setenv("CELERY_BEAT_REFRESH_SECONDS", "60")

        # Mock SessionLocal
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://broker:6379/0"
        assert config["result_backend"] == "redis://backend:6379/1"
        assert config["timezone"] == "America/New_York"
        assert config["beat_max_loop_interval"] == 10
        assert config["beat_refresh_seconds"] == 60

    def test_uses_redis_url_fallback(self, monkeypatch):
        """Test uses REDIS_URL as fallback for broker/backend."""
        for var in ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379/0")

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://fallback:6379/0"
        assert config["result_backend"] == "redis://fallback:6379/0"


# =============================================================================
# Build Beat Schedule Tests
# =============================================================================


class TestBuildBeatSchedule:
    """Tests for build_beat_schedule function."""

    def test_builds_gis_sync_schedule(self, monkeypatch):
        """Test builds GIS sync schedule when enabled."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "true")
        monkeypatch.setenv("GIS_SYNC_INTERVAL_MINUTES", "30")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        # Return None for all setting queries
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        # Return empty list for tasks
        mock_session.query.return_value.filter.return_value.all.return_value = []
        # Mock order_by for _sync_scheduled_task
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(scheduler_config.integration_service, "list_interval_jobs", return_value=[]):
                schedule = scheduler_config.build_beat_schedule()

        assert "gis_sync" in schedule
        assert schedule["gis_sync"]["task"] == "app.tasks.gis.sync_gis_sources"
        assert schedule["gis_sync"]["schedule"] == timedelta(minutes=30)

    def test_excludes_gis_sync_when_disabled(self, monkeypatch):
        """Test excludes GIS sync schedule when disabled."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(scheduler_config.integration_service, "list_interval_jobs", return_value=[]):
                schedule = scheduler_config.build_beat_schedule()

        assert "gis_sync" not in schedule

    def test_builds_integration_job_schedules(self, monkeypatch):
        """Test builds integration job schedules."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        # Mock integration job
        mock_job = MagicMock()
        mock_job.id = "job-123"
        mock_job.interval_minutes = 15

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(scheduler_config.integration_service, "list_interval_jobs", return_value=[mock_job]):
                schedule = scheduler_config.build_beat_schedule()

        assert "integration_job_job-123" in schedule
        assert schedule["integration_job_job-123"]["task"] == "app.tasks.integrations.run_integration_job"
        assert schedule["integration_job_job-123"]["schedule"] == timedelta(minutes=15)
        assert schedule["integration_job_job-123"]["args"] == ["job-123"]

    def test_builds_scheduled_task_schedules(self, monkeypatch):
        """Test builds scheduled task schedules."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_task = MagicMock()
        mock_task.id = "task-456"
        mock_task.task_name = "app.tasks.custom.run"
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 900
        mock_task.args_json = ["arg1"]
        mock_task.kwargs_json = {"key": "value"}

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_task]
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(scheduler_config.integration_service, "list_interval_jobs", return_value=[]):
                schedule = scheduler_config.build_beat_schedule()

        assert "scheduled_task_task-456" in schedule
        assert schedule["scheduled_task_task-456"]["task"] == "app.tasks.custom.run"
        assert schedule["scheduled_task_task-456"]["schedule"] == timedelta(seconds=900)
        assert schedule["scheduled_task_task-456"]["args"] == ["arg1"]
        assert schedule["scheduled_task_task-456"]["kwargs"] == {"key": "value"}

    def test_minimum_interval_enforcement(self, monkeypatch):
        """Test minimum interval is enforced for GIS sync."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "true")
        monkeypatch.setenv("GIS_SYNC_INTERVAL_MINUTES", "0")  # Should become 1
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(scheduler_config.integration_service, "list_interval_jobs", return_value=[]):
                schedule = scheduler_config.build_beat_schedule()

        assert schedule["gis_sync"]["schedule"] == timedelta(minutes=1)

    def test_handles_exception_gracefully(self, monkeypatch, caplog):
        """Test handles database exceptions gracefully."""
        monkeypatch.delenv("GIS_SYNC_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("Database error")

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            schedule = scheduler_config.build_beat_schedule()

        # Should return empty schedule without crashing
        assert schedule == {}
