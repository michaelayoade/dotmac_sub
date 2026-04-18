"""Tests for OLT feature gap fixes.

Covers: event system integration, ONT status transitions, SNMP credential
resolution, firmware version extraction, DeviceStatus enum, backup retention,
and multi-vendor signal parsing.
"""

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from app.models.network import (
    DeviceStatus,
    OltConfigBackup,
    OltConfigBackupType,
)
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services.events.types import EventType
from app.services.network.olt_operations import validate_cli_command
from app.services.network.olt_polling import (
    _derive_offline_reason,
    _parse_online_status,
    _parse_signal_value,
    classify_signal,
)
from app.services.network.olt_ssh_ont._common import OntStatusEntry, RegisteredOntEntry

# ---------------------------------------------------------------------------
# 1. Event type definitions
# ---------------------------------------------------------------------------


class TestOltEventTypes:
    """Verify OLT/ONT event types are registered."""

    def test_olt_created_event_exists(self) -> None:
        assert EventType.olt_created.value == "olt.created"

    def test_olt_updated_event_exists(self) -> None:
        assert EventType.olt_updated.value == "olt.updated"

    def test_olt_deleted_event_exists(self) -> None:
        assert EventType.olt_deleted.value == "olt.deleted"

    def test_ont_discovered_event_exists(self) -> None:
        assert EventType.ont_discovered.value == "ont.discovered"

    def test_ont_online_event_exists(self) -> None:
        assert EventType.ont_online.value == "ont.online"

    def test_ont_offline_event_exists(self) -> None:
        assert EventType.ont_offline.value == "ont.offline"

    def test_ont_signal_degraded_event_exists(self) -> None:
        assert EventType.ont_signal_degraded.value == "ont.signal_degraded"


class TestOltCliValidation:
    """Verify read-only OLT CLI commands needed by the UI remain allowed."""

    def test_display_current_configuration_is_allowed(self) -> None:
        assert validate_cli_command("display current-configuration") is None

    def test_config_mode_is_rejected(self) -> None:
        assert validate_cli_command("config") is not None


class TestOltOntStatusBySerial:
    """Verify OLT-level ONT status lookup can start from a serial number."""

    def test_lookup_by_serial_chains_to_full_ont_status(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network import olt_operations

        calls: list[tuple[str, object]] = []
        olt = SimpleNamespace(id=uuid.uuid4(), name="OLT-A")

        def _find_ont_by_serial(_olt, serial_number):
            calls.append(("find", serial_number))
            return (
                True,
                "found",
                RegisteredOntEntry(
                    fsp="0/1/6",
                    onu_id=5,
                    real_serial=serial_number,
                    run_state="online",
                ),
            )

        def _get_ont_status(_olt, fsp, ont_id):
            calls.append(("status", (fsp, ont_id)))
            return (
                True,
                "ONT status retrieved",
                OntStatusEntry(
                    serial_number="HWTC28201B9A",
                    run_state="online",
                    config_state="normal",
                    match_state="match",
                ),
            )

        monkeypatch.setattr(olt_operations, "get_olt_or_none", lambda *_args: olt)
        monkeypatch.setattr(
            olt_operations, "log_olt_audit_event", lambda *_args, **_kwargs: None
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.find_ont_by_serial",
            _find_ont_by_serial,
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_ont_status",
            _get_ont_status,
        )

        ok, message, status = olt_operations.get_ont_status_by_serial(
            db_session, str(olt.id), "4857544328201B9A"
        )

        assert ok is True
        assert "0/1/6" in message
        assert status["requested_serial"] == "4857544328201B9A"
        assert status["lookup_serial"] == "4857544328201B9A"
        assert status["fsp"] == "0/1/6"
        assert status["ont_id"] == 5
        assert status["run_state"] == "online"
        assert status["config_state"] == "normal"
        assert status["match_state"] == "match"
        assert calls == [
            ("find", "4857544328201B9A"),
            ("status", ("0/1/6", 5)),
        ]

    def test_lookup_by_hex_serial_tries_huawei_display_serial_variant(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network import olt_operations

        calls: list[tuple[str, object]] = []
        olt = SimpleNamespace(id=uuid.uuid4(), name="OLT-A")

        def _find_ont_by_serial(_olt, serial_number):
            calls.append(("find", serial_number))
            if serial_number == "4857544328201B9A":
                return True, "not found", None
            return (
                True,
                "found",
                RegisteredOntEntry(
                    fsp="0/1/6",
                    onu_id=5,
                    real_serial=serial_number,
                    run_state="online",
                ),
            )

        def _get_ont_status(_olt, fsp, ont_id):
            calls.append(("status", (fsp, ont_id)))
            return (
                True,
                "ONT status retrieved",
                OntStatusEntry(
                    serial_number="HWTC28201B9A",
                    run_state="online",
                    config_state="normal",
                    match_state="match",
                ),
            )

        monkeypatch.setattr(olt_operations, "get_olt_or_none", lambda *_args: olt)
        monkeypatch.setattr(
            olt_operations, "log_olt_audit_event", lambda *_args, **_kwargs: None
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.find_ont_by_serial",
            _find_ont_by_serial,
        )
        monkeypatch.setattr(
            "app.services.network.olt_ssh_ont.get_ont_status",
            _get_ont_status,
        )

        ok, _message, status = olt_operations.get_ont_status_by_serial(
            db_session, str(olt.id), "4857544328201B9A"
        )

        assert ok is True
        assert status["requested_serial"] == "4857544328201B9A"
        assert status["lookup_serial"] == "HWTC28201B9A"
        assert calls == [
            ("find", "4857544328201B9A"),
            ("find", "HWTC28201B9A"),
            ("status", ("0/1/6", 5)),
        ]

    def test_lookup_by_serial_rejects_unsafe_serial(
        self, db_session, monkeypatch
    ) -> None:
        from app.services.network import olt_operations

        olt = SimpleNamespace(id=uuid.uuid4(), name="OLT-A")
        monkeypatch.setattr(olt_operations, "get_olt_or_none", lambda *_args: olt)
        monkeypatch.setattr(
            olt_operations, "log_olt_audit_event", lambda *_args, **_kwargs: None
        )

        ok, message, status = olt_operations.get_ont_status_by_serial(
            db_session, str(olt.id), "4857544328201B9A;reboot"
        )

        assert ok is False
        assert "may only contain" in message
        assert status == {}


# ---------------------------------------------------------------------------
# 2. OLT CRUD emits events
# ---------------------------------------------------------------------------


class TestOltCrudEvents:
    """Verify OLT CRUD operations emit events."""

    def test_create_olt_emits_event(self, db_session) -> None:
        with patch("app.services.network.olt.emit_event") as mock_emit:
            from app.services.network.olt import OLTDevices

            device = OLTDevices.create(
                db_session,
                OLTDeviceCreate(name="Event Test OLT"),
            )
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            assert call_args[0][1] == EventType.olt_created
            assert call_args[0][2]["name"] == "Event Test OLT"

    def test_update_olt_emits_event(self, db_session) -> None:
        from app.services.network.olt import OLTDevices

        device = OLTDevices.create(
            db_session,
            OLTDeviceCreate(name="Before Update"),
        )
        with patch("app.services.network.olt.emit_event") as mock_emit:
            OLTDevices.update(
                db_session,
                str(device.id),
                OLTDeviceUpdate(name="After Update"),
            )
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][1] == EventType.olt_updated

    def test_delete_olt_emits_event(self, db_session) -> None:
        from app.services.network.olt import OLTDevices

        device = OLTDevices.create(
            db_session,
            OLTDeviceCreate(name="To Delete"),
        )
        with patch("app.services.network.olt.emit_event") as mock_emit:
            OLTDevices.delete(db_session, str(device.id))
            mock_emit.assert_called_once()
            assert mock_emit.call_args[0][1] == EventType.olt_deleted


# ---------------------------------------------------------------------------
# 3. Multi-vendor signal parsing
# ---------------------------------------------------------------------------


class TestMultiVendorSignalParsing:
    """Test signal value parsing across vendors."""

    def test_zte_olt_rx_signal(self) -> None:
        value = _parse_signal_value("-2100", vendor="zte", metric="olt_rx")
        assert value == -21.0

    def test_zte_onu_rx_signal(self) -> None:
        value = _parse_signal_value("-2500", vendor="zte", metric="onu_rx")
        assert value == -25.0

    def test_nokia_olt_rx_signal(self) -> None:
        value = _parse_signal_value("-1800", vendor="nokia", metric="olt_rx")
        assert value == -18.0

    def test_generic_olt_rx_signal(self) -> None:
        value = _parse_signal_value("-2200", vendor="generic", metric="olt_rx")
        assert value == -22.0

    def test_huawei_onu_rx_large_value(self) -> None:
        # Huawei reports offset values for ONU Rx > 1000
        value = _parse_signal_value("7500", vendor="huawei", metric="onu_rx")
        assert value == -25.0

    def test_huawei_olt_rx_normal_scale(self) -> None:
        value = _parse_signal_value("-2000", vendor="huawei", metric="olt_rx")
        assert value == -20.0

    def test_sentinel_value_returns_none(self) -> None:
        for sentinel in [2147483647, 65535, -2147483648]:
            value = _parse_signal_value(str(sentinel), vendor="huawei", metric="olt_rx")
            assert value is None

    def test_out_of_range_value_returns_none(self) -> None:
        value = _parse_signal_value("-999999", vendor="zte", metric="olt_rx")
        assert value is None


# ---------------------------------------------------------------------------
# 4. Online status parsing
# ---------------------------------------------------------------------------


class TestOnlineStatusParsing:
    def test_code_1_is_online(self) -> None:
        assert _parse_online_status("1") is True

    def test_code_2_is_offline(self) -> None:
        assert _parse_online_status("2") is False

    def test_code_3_is_offline(self) -> None:
        assert _parse_online_status("3") is False

    def test_text_online(self) -> None:
        assert _parse_online_status("online") is True

    def test_text_offline(self) -> None:
        assert _parse_online_status("offline") is False


class TestOfflineReasonDerivation:
    def test_code_3_is_power_fail(self) -> None:
        assert _derive_offline_reason("3") == "power_fail"

    def test_code_4_is_los(self) -> None:
        assert _derive_offline_reason("4") == "los"

    def test_code_5_is_dying_gasp(self) -> None:
        assert _derive_offline_reason("5") == "dying_gasp"

    def test_code_1_is_none(self) -> None:
        assert _derive_offline_reason("1") is None


# ---------------------------------------------------------------------------
# 5. Signal classification
# ---------------------------------------------------------------------------


class TestSignalClassification:
    def test_good_signal(self) -> None:
        assert classify_signal(-20.0) == "good"

    def test_warning_signal(self) -> None:
        assert classify_signal(-26.0) == "warning"

    def test_critical_signal(self) -> None:
        assert classify_signal(-30.0) == "critical"

    def test_none_signal_is_unknown(self) -> None:
        assert classify_signal(None) == "unknown"

    def test_custom_thresholds(self) -> None:
        assert (
            classify_signal(-22.0, warn_threshold=-20.0, crit_threshold=-24.0)
            == "warning"
        )
        assert (
            classify_signal(-25.0, warn_threshold=-20.0, crit_threshold=-24.0)
            == "critical"
        )


# ---------------------------------------------------------------------------
# 6. DeviceStatus enum on OLTDevice
# ---------------------------------------------------------------------------


class TestDeviceStatusEnum:
    def test_enum_values(self) -> None:
        assert DeviceStatus.active.value == "active"
        assert DeviceStatus.inactive.value == "inactive"
        assert DeviceStatus.maintenance.value == "maintenance"
        assert DeviceStatus.retired.value == "retired"

    def test_olt_device_with_status(self, db_session) -> None:
        from app.services.network.olt import OLTDevices

        device = OLTDevices.create(
            db_session,
            OLTDeviceCreate(name="Status Test OLT", status="active"),
        )
        assert device.status == DeviceStatus.active

    def test_olt_device_update_status_to_maintenance(self, db_session) -> None:
        from app.services.network.olt import OLTDevices

        device = OLTDevices.create(
            db_session,
            OLTDeviceCreate(name="Maintenance OLT"),
        )
        updated = OLTDevices.update(
            db_session,
            str(device.id),
            OLTDeviceUpdate(status="maintenance"),
        )
        assert updated.status == DeviceStatus.maintenance


# ---------------------------------------------------------------------------
# 7. Firmware version extraction
# ---------------------------------------------------------------------------


class TestFirmwareExtraction:
    def test_huawei_vrp_version(self) -> None:
        from app.services.web_network_olts import _extract_firmware_version

        output = """Huawei Versatile Routing Platform Software
VRP (R) software, Version V800R021C10SPC100
"""
        fw = _extract_firmware_version(output)
        assert fw is not None
        assert "V800R021" in fw

    def test_generic_version_line(self) -> None:
        from app.services.web_network_olts import _extract_firmware_version

        output = "Software Version: 12.3.4-build567"
        fw = _extract_firmware_version(output)
        assert fw == "12.3.4-build567"

    def test_no_version_returns_none(self) -> None:
        from app.services.web_network_olts import _extract_firmware_version

        fw = _extract_firmware_version("No useful info here")
        assert fw is None


# ---------------------------------------------------------------------------
# 8. OLT schema fields
# ---------------------------------------------------------------------------


class TestOltSchemaFields:
    def test_create_schema_has_firmware_fields(self) -> None:
        schema = OLTDeviceCreate(
            name="Test",
            firmware_version="v1.2.3",
            software_version="sw-4.5",
        )
        assert schema.firmware_version == "v1.2.3"
        assert schema.software_version == "sw-4.5"

    def test_create_schema_has_status_field(self) -> None:
        schema = OLTDeviceCreate(name="Test", status="maintenance")
        assert schema.status == "maintenance"

    def test_update_schema_has_firmware_fields(self) -> None:
        schema = OLTDeviceUpdate(firmware_version="v2.0")
        data = schema.model_dump(exclude_unset=True)
        assert "firmware_version" in data


# ---------------------------------------------------------------------------
# 9. Backup integrity hash
# ---------------------------------------------------------------------------


class TestBackupIntegrity:
    def test_sha256_hash_computed(self) -> None:
        config_text = "# OLT Config\nsysname test-olt\n"
        expected_hash = hashlib.sha256(config_text.encode()).hexdigest()
        assert len(expected_hash) == 64

    def test_backup_model_has_file_hash(self) -> None:
        backup = OltConfigBackup(
            id=uuid.uuid4(),
            olt_device_id=uuid.uuid4(),
            backup_type=OltConfigBackupType.auto,
            file_path="test/backup.txt",
            file_size_bytes=100,
            file_hash="abc123",
        )
        assert backup.file_hash == "abc123"


# ---------------------------------------------------------------------------
# 10. Notification handler template mapping
# ---------------------------------------------------------------------------


class TestNotificationTemplateMapping:
    def test_ont_events_mapped_to_templates(self) -> None:
        from app.services.events.handlers.notification import EVENT_TYPE_TO_TEMPLATE

        assert EVENT_TYPE_TO_TEMPLATE[EventType.ont_offline] == "ont_offline"
        assert EVENT_TYPE_TO_TEMPLATE[EventType.ont_online] == "ont_online"
        assert (
            EVENT_TYPE_TO_TEMPLATE[EventType.ont_signal_degraded]
            == "ont_signal_degraded"
        )
        assert EVENT_TYPE_TO_TEMPLATE[EventType.ont_discovered] == "ont_discovered"
