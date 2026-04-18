"""Tests for NAS gap fixes.

Covers: event emission, scheduled backup task, capacity tracking, health check,
provisioning timeout, config restore, and Celery task registration.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from app.services.events.types import EventType

# ---------------------------------------------------------------------------
# 1. NAS event types
# ---------------------------------------------------------------------------


class TestNasEventTypes:
    def test_nas_device_created_event(self) -> None:
        assert EventType.nas_device_created.value == "nas_device.created"

    def test_nas_device_updated_event(self) -> None:
        assert EventType.nas_device_updated.value == "nas_device.updated"

    def test_nas_device_deleted_event(self) -> None:
        assert EventType.nas_device_deleted.value == "nas_device.deleted"

    def test_nas_backup_completed_event(self) -> None:
        assert EventType.nas_backup_completed.value == "nas_backup.completed"

    def test_nas_backup_failed_event(self) -> None:
        assert EventType.nas_backup_failed.value == "nas_backup.failed"

    def test_nas_provisioning_completed_event(self) -> None:
        assert (
            EventType.nas_provisioning_completed.value == "nas_provisioning.completed"
        )

    def test_nas_provisioning_failed_event(self) -> None:
        assert EventType.nas_provisioning_failed.value == "nas_provisioning.failed"


# ---------------------------------------------------------------------------
# 2. Event emission helper
# ---------------------------------------------------------------------------


class TestNasEventEmission:
    def test_emit_nas_event_nonblocking(self) -> None:
        from app.services.nas._helpers import _emit_nas_event

        db = MagicMock()
        # Should not raise even with mock db
        _emit_nas_event(db, "nas_device_created", {"device_id": "test"})

    def test_emit_nas_event_unknown_type_ignored(self) -> None:
        from app.services.nas._helpers import _emit_nas_event

        db = MagicMock()
        _emit_nas_event(db, "nonexistent_event_xyz", {})


# ---------------------------------------------------------------------------
# 3. Celery task registration
# ---------------------------------------------------------------------------


class TestNasCeleryTasks:
    def test_cleanup_nas_backups_registered(self) -> None:
        from app.tasks.nas import cleanup_nas_backups

        assert cleanup_nas_backups.name == "app.tasks.nas.cleanup_nas_backups"

    def test_run_scheduled_backups_registered(self) -> None:
        from app.tasks.nas import run_scheduled_backups

        assert run_scheduled_backups.name == "app.tasks.nas.run_scheduled_backups"

    def test_update_subscriber_counts_registered(self) -> None:
        from app.tasks.nas import update_subscriber_counts

        assert update_subscriber_counts.name == "app.tasks.nas.update_subscriber_counts"

    def test_check_nas_health_registered(self) -> None:
        from app.tasks.nas import check_nas_health

        assert check_nas_health.name == "app.tasks.nas.check_nas_health"

    def test_tasks_in_init_all(self) -> None:
        from app.tasks import __all__ as all_tasks

        assert "cleanup_nas_backups" in all_tasks
        assert "run_scheduled_backups" in all_tasks
        assert "update_subscriber_counts" in all_tasks
        assert "check_nas_health" in all_tasks


# ---------------------------------------------------------------------------
# 4. Backup schedule parsing
# ---------------------------------------------------------------------------


class TestBackupScheduleParsing:
    def test_daily_keyword(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("daily")
        assert result == timedelta(hours=24)

    def test_weekly_keyword(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("weekly")
        assert result == timedelta(days=7)

    def test_hours_format(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("12h")
        assert result == timedelta(hours=12)

    def test_days_format(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("3d")
        assert result == timedelta(days=3)

    def test_none_defaults_to_24h(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval(None)
        assert result == timedelta(hours=24)

    def test_empty_string_defaults(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("")
        assert result == timedelta(hours=24)

    def test_unknown_format_defaults(self) -> None:
        from app.tasks.nas import _parse_backup_interval

        result = _parse_backup_interval("every tuesday")
        assert result == timedelta(hours=24)


# ---------------------------------------------------------------------------
# 5. Provisioning timeout enforcement
# ---------------------------------------------------------------------------


class TestProvisioningTimeout:
    def test_execute_ssh_accepts_timeout(self) -> None:
        """Verify _execute_ssh signature accepts timeout_seconds."""
        import inspect

        from app.services.nas.provisioner import DeviceProvisioner

        sig = inspect.signature(DeviceProvisioner._execute_ssh)
        assert "timeout_seconds" in sig.parameters

    def test_execute_api_accepts_timeout(self) -> None:
        """Verify _execute_api signature accepts timeout_seconds."""
        import inspect

        from app.services.nas.provisioner import DeviceProvisioner

        sig = inspect.signature(DeviceProvisioner._execute_api)
        assert "timeout_seconds" in sig.parameters

    def test_execute_ssh_default_timeout(self) -> None:
        import inspect

        from app.services.nas.provisioner import DeviceProvisioner

        sig = inspect.signature(DeviceProvisioner._execute_ssh)
        default = sig.parameters["timeout_seconds"].default
        assert default == 60


# ---------------------------------------------------------------------------
# 6. Config restore method exists
# ---------------------------------------------------------------------------


class TestConfigRestore:
    def test_restore_config_method_exists(self) -> None:
        from app.services.nas.provisioner import DeviceProvisioner

        assert hasattr(DeviceProvisioner, "restore_config")
        assert callable(DeviceProvisioner.restore_config)

    def test_restore_config_signature(self) -> None:
        import inspect

        from app.services.nas.provisioner import DeviceProvisioner

        sig = inspect.signature(DeviceProvisioner.restore_config)
        params = list(sig.parameters.keys())
        assert "db" in params
        assert "nas_device_id" in params
        assert "backup_id" in params
        assert "triggered_by" in params


# ---------------------------------------------------------------------------
# 7. NAS package refactoring — backward compatibility
# ---------------------------------------------------------------------------


class TestNasPackageCompat:
    def test_import_via_module(self) -> None:
        from app.services import nas as nas_service

        assert hasattr(nas_service, "NasDevices")
        assert hasattr(nas_service, "DeviceProvisioner")
        assert hasattr(nas_service, "NasConfigBackups")

    def test_import_classes_directly(self) -> None:
        from app.services.nas import (
            DeviceProvisioner,
            NasDevices,
        )

        assert NasDevices is not None
        assert DeviceProvisioner is not None

    def test_singletons_available(self) -> None:
        from app.services.nas import (
            device_provisioner,
            nas_devices,
        )

        assert nas_devices is not None
        assert device_provisioner is not None

    def test_helper_functions_available(self) -> None:
        from app.services.nas import (
            get_nas_form_options,
            validate_ipv4_address,
        )

        assert callable(validate_ipv4_address)
        assert callable(get_nas_form_options)

    def test_schemas_reexported(self) -> None:
        from app.services.nas import (
            NasDeviceCreate,
        )

        assert NasDeviceCreate is not None

    def test_catalog_compat_layer(self) -> None:
        """catalog/nas.py re-exports from nas package."""
        from app.services.catalog.nas import NasDevices

        assert NasDevices is not None


# ---------------------------------------------------------------------------
# 8. Redact sensitive fields
# ---------------------------------------------------------------------------


class TestRedactSensitive:
    def test_redacts_password(self) -> None:
        from app.services.nas import _redact_sensitive

        data = {"username": "admin", "password": "secret123", "host": "192.168.1.1"}
        result = _redact_sensitive(data)
        assert result["username"] == "admin"
        assert "***" in result["password"]
        assert result["host"] == "192.168.1.1"

    def test_redacts_secret(self) -> None:
        from app.services.nas import _redact_sensitive

        data = {"shared_secret": "mysecret", "name": "test"}
        result = _redact_sensitive(data)
        assert "***" in result["shared_secret"]
        assert result["name"] == "test"

    def test_preserves_non_sensitive(self) -> None:
        from app.services.nas import _redact_sensitive

        data = {"username": "admin", "ip": "10.0.0.1"}
        result = _redact_sensitive(data)
        assert result == data
