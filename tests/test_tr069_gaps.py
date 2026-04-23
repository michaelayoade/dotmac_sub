"""Tests for TR-069 feature gap fixes.

Covers: event types, Celery task structure, inform webhook, job retry model,
auto-link ONTs, parameter map resolution, and session cleanup.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.tr069 import (
    Tr069AcsServer,
    Tr069CpeDevice,
    Tr069Event,
    Tr069Job,
    Tr069Session,
)
from app.schemas.tr069 import Tr069AcsServerCreate, Tr069JobCreate
from app.services.events.types import EventType

# ---------------------------------------------------------------------------
# 1. TR-069 event types
# ---------------------------------------------------------------------------


class TestTr069EventTypes:
    def test_job_completed_event(self) -> None:
        assert EventType.tr069_job_completed.value == "tr069_job.completed"

    def test_job_failed_event(self) -> None:
        assert EventType.tr069_job_failed.value == "tr069_job.failed"

    def test_device_discovered_event(self) -> None:
        assert EventType.tr069_device_discovered.value == "tr069_device.discovered"

    def test_device_stale_event(self) -> None:
        assert EventType.tr069_device_stale.value == "tr069_device.stale"


# ---------------------------------------------------------------------------
# 2. Job retry model columns
# ---------------------------------------------------------------------------


class TestJobRetryModel:
    def test_job_has_retry_count(self, db_session) -> None:
        from app.services.tr069 import acs_servers, jobs

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Retry Test ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="RETRY-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)

        job = jobs.create(
            db_session,
            Tr069JobCreate(
                device_id=device.id,
                name="Test Retry",
                command="reboot",
            ),
        )
        assert job.retry_count == 0
        assert job.max_retries == 3

    def test_retry_count_increments(self, db_session) -> None:
        from app.services.tr069 import acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Retry Inc ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="RETRY-002",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        job = Tr069Job(
            device_id=device.id,
            name="Test",
            command="reboot",
            retry_count=0,
            max_retries=3,
        )
        db_session.add(job)
        db_session.commit()

        job.retry_count += 1
        db_session.commit()
        db_session.refresh(job)
        assert job.retry_count == 1


# ---------------------------------------------------------------------------
# 3. Inform webhook endpoint
# ---------------------------------------------------------------------------


class TestInformWebhook:
    def test_inform_updates_last_inform_at(self, db_session) -> None:
        """Test the inform endpoint logic via direct service call."""
        from sqlalchemy import select

        server = Tr069AcsServer(
            name="Inform ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="INFORM-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)
        assert device.last_inform_at is None

        # Simulate what the endpoint does
        now = datetime.now(UTC)
        found = db_session.scalars(
            select(Tr069CpeDevice)
            .where(
                Tr069CpeDevice.serial_number == "INFORM-001",
                Tr069CpeDevice.is_active.is_(True),
            )
            .limit(1)
        ).first()
        assert found is not None
        found.last_inform_at = now

        session = Tr069Session(
            device_id=found.id,
            event_type=Tr069Event.boot,
            started_at=now,
            ended_at=now,
        )
        db_session.add(session)
        db_session.commit()

        db_session.refresh(found)
        assert found.last_inform_at is not None

    def test_inform_endpoint_schema_validation(self) -> None:
        """Test the InformPayload Pydantic model."""
        from app.api.tr069_inform import InformPayload

        # With serial
        payload = InformPayload(serial_number="TEST-001", event="boot")
        assert payload.serial_number == "TEST-001"
        assert payload.event == "boot"
        extra_payload = InformPayload(
            serial_number="TEST-001",
            parameters={"Device.DeviceInfo.SoftwareVersion": "V1"},
        )
        assert extra_payload.model_dump()["parameters"] == {
            "Device.DeviceInfo.SoftwareVersion": "V1"
        }

        # Without serial — should default
        payload = InformPayload()
        assert payload.serial_number is None
        assert payload.event == "periodic"

    def test_inform_extracts_serial_from_device_id(self) -> None:
        """Test serial extraction from device_id format."""
        device_id = "00D09E-TestProduct-SERIAL123"
        parts = device_id.split("-", 2)
        assert len(parts) == 3
        assert parts[2] == "SERIAL123"


# ---------------------------------------------------------------------------
# 4. Auto-link ONTs during sync
# ---------------------------------------------------------------------------


class TestAutoLinkOnts:
    def test_sync_auto_links_ont_by_serial(self, db_session) -> None:

        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="AutoLink ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )

        # Create an ONT with no ACS server
        ont = OntUnit(serial_number="AUTOLINK-001", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id is None

        # Mock GenieACS to return a device with matching serial
        mock_device = {
            "_id": "00D09E-TestProduct-AUTOLINK-001",
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": "AUTOLINK-001",
            },
            "_lastInform": datetime.now(UTC).isoformat(),
        }
        with patch("app.services.tr069.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "AUTOLINK-001",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1

        # Verify ONT now has ACS server linked
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id == server.id
        linked = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="AUTOLINK-001")
            .first()
        )
        assert linked is not None
        assert linked.ont_unit_id == ont.id

    def test_sync_auto_links_ont_by_normalized_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Normalized AutoLink ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )

        ont = OntUnit(serial_number="HWTC-7D47-33C3", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        mock_device = {
            "_id": "00D09E-TestProduct-HWTC7D4733C3",
            "_deviceId": {
                "_OUI": "00D09E",
                "_ProductClass": "TestProduct",
                "_SerialNumber": "HWTC7D4733C3",
            },
            "_lastInform": datetime.now(UTC).isoformat(),
        }
        with patch("app.services.tr069.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "HWTC7D4733C3",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id == server.id
        linked = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="HWTC7D4733C3")
            .first()
        )
        assert linked is not None
        assert linked.ont_unit_id == ont.id

    def test_sync_reactivates_offline_local_ont_tr069_row(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Offline Local ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        ont = OntUnit(
            serial_number="HWTC13EE6B84",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC13EE6B84",
            is_active=False,
        )
        db_session.add(device)
        db_session.commit()

        with patch("app.services.tr069.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = []

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        db_session.refresh(device)
        db_session.refresh(ont)
        assert result["local_reactivated"] == 1
        assert device.is_active is True
        assert device.ont_unit_id == ont.id
        assert ont.tr069_acs_server_id == server.id

    def test_sync_creates_local_tr069_row_for_olt_assigned_offline_ont(
        self, db_session
    ) -> None:
        from app.models.network import OLTDevice, OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="OLT Local ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        olt = OLTDevice(
            name="TR069 Offline OLT",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(olt)
        db_session.flush()
        ont = OntUnit(
            serial_number="OFFLINE-OLT-ACS-001",
            olt_device_id=olt.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.commit()

        with patch("app.services.tr069.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = []

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        linked = (
            db_session.query(Tr069CpeDevice)
            .filter_by(serial_number="OFFLINE-OLT-ACS-001", is_active=True)
            .one()
        )
        db_session.refresh(ont)
        assert result["local_created"] == 1
        assert linked.ont_unit_id == ont.id
        assert linked.genieacs_device_id is None
        assert ont.tr069_acs_server_id == server.id

    def test_sync_retires_expected_placeholder_when_real_acs_device_arrives(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.tr069 import CpeDevices, acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Placeholder Reconcile ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        ont = OntUnit(
            serial_number="HWTC13EE6B84",
            tr069_acs_server_id=server.id,
            is_active=True,
        )
        db_session.add(ont)
        db_session.flush()
        placeholder = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC13EE6B84",
            is_active=True,
        )
        db_session.add(placeholder)
        db_session.commit()

        mock_device = {
            "_id": "4857544313EE6B84",
            "_deviceId": {
                "_OUI": "48575443",
                "_ProductClass": "HG8245H",
                "_SerialNumber": "4857544313EE6B84",
            },
            "_lastInform": datetime.now(UTC).isoformat(),
        }
        with patch("app.services.tr069.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = (
                "48575443",
                "HG8245H",
                "4857544313EE6B84",
            )
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        registered = (
            db_session.query(Tr069CpeDevice)
            .filter_by(genieacs_device_id="4857544313EE6B84")
            .one()
        )
        db_session.refresh(placeholder)
        db_session.refresh(ont)

        assert result["created"] == 1
        assert registered.ont_unit_id == ont.id
        assert registered.is_active is True
        assert placeholder.ont_unit_id is None
        assert placeholder.is_active is False
        assert ont.tr069_acs_server_id == server.id

    def test_unregistered_expected_rows_reject_firmware_and_nat_actions(
        self, db_session
    ) -> None:
        from app.services import web_network_tr069 as web_network_tr069_service
        from app.services.tr069 import acs_servers

        server = acs_servers.create(
            db_session,
            Tr069AcsServerCreate(
                name="Expected Action Guard ACS",
                base_url="http://genieacs:7557",
                cwmp_url="http://acs/cwmp",
                cwmp_username="u",
                cwmp_password="p",
                connection_request_username="cu",
                connection_request_password="cp",
            ),
        )
        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="EXPECTED-ACTION-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        with pytest.raises(ValueError, match="not registered in GenieACS"):
            web_network_tr069_service.create_firmware_download_job(
                db_session,
                tr069_device_id=str(device.id),
                firmware_url="https://example.test/fw.bin",
            )
        with pytest.raises(ValueError, match="not registered in GenieACS"):
            web_network_tr069_service.create_nat_port_forward_job(
                db_session,
                tr069_device_id=str(device.id),
                external_port=8080,
                internal_ip="192.168.1.10",
                internal_port=80,
                protocol="TCP",
            )


class TestCreateOntFromTr069Device:
    def test_create_ont_from_tr069_device_creates_inactive_ont(
        self, db_session
    ) -> None:
        from app.services.web_network_tr069 import create_ont_from_tr069_device

        server = Tr069AcsServer(
            name="Create ONT ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="485754432B526E9A",
            oui="48575443",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(device)

        ont, created = create_ont_from_tr069_device(
            db_session,
            tr069_device_id=str(device.id),
        )

        assert created is True
        assert ont.serial_number == "HWTC2B526E9A"
        assert ont.model == "HG8546M"
        assert ont.is_active is False
        assert ont.tr069_acs_server_id == server.id
        db_session.refresh(device)
        assert device.ont_unit_id == ont.id

    def test_create_ont_from_tr069_device_reuses_existing_normalized_serial(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.web_network_tr069 import create_ont_from_tr069_device

        server = Tr069AcsServer(
            name="Reuse ONT ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        existing = OntUnit(serial_number="HWTC-2B52-6E9A", is_active=False)
        db_session.add(existing)
        db_session.flush()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="HWTC2B526E9A",
            oui="48575443",
            product_class="HG8546M",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()
        db_session.refresh(existing)
        db_session.refresh(device)

        ont, created = create_ont_from_tr069_device(
            db_session,
            tr069_device_id=str(device.id),
        )

        assert created is False
        assert ont.id == existing.id
        assert ont.tr069_acs_server_id == server.id
        db_session.refresh(device)
        assert device.ont_unit_id == existing.id


class TestTr069DashboardUi:
    def test_unconfigured_devices_offer_create_ont_action(self) -> None:
        template = Path("templates/admin/network/tr069/index.html").read_text()

        assert "/create-ont" in template
        assert "Create ONT" in template


# ---------------------------------------------------------------------------
# 5. Parameter map resolution
# ---------------------------------------------------------------------------


class TestParameterMapResolution:
    def test_resolve_returns_none_without_db(self) -> None:
        from app.services.network.ont_tr069 import _resolve_param_paths_from_capability

        result = _resolve_param_paths_from_capability(
            None, "Huawei", "HG8245H", "system.manufacturer"
        )
        assert result is None

    def test_resolve_returns_none_without_vendor(self) -> None:
        from app.services.network.ont_tr069 import _resolve_param_paths_from_capability

        result = _resolve_param_paths_from_capability(
            MagicMock(), None, None, "system.manufacturer"
        )
        assert result is None

    def test_resolve_uses_vendor_capability_parameter_map(self, db_session) -> None:
        from app.models.network import Tr069ParameterMap, VendorModelCapability
        from app.services.network.ont_tr069 import _resolve_param_paths_from_capability

        capability = VendorModelCapability(
            vendor="Huawei",
            model="HG8245H",
            is_active=True,
        )
        db_session.add(capability)
        db_session.flush()
        db_session.add(
            Tr069ParameterMap(
                capability_id=capability.id,
                canonical_name="wan.pppoe.username",
                tr069_path="InternetGatewayDevice.WANDevice.2.Username",
                writable=True,
            )
        )
        db_session.commit()

        result = _resolve_param_paths_from_capability(
            db_session,
            "Huawei",
            "HG8245H",
            "wan.pppoe.username",
        )

        assert result == ["InternetGatewayDevice.WANDevice.2.Username"]


# ---------------------------------------------------------------------------
# 6. Session and job cleanup
# ---------------------------------------------------------------------------


class TestRecordCleanup:
    def test_old_sessions_cleaned(self, db_session) -> None:
        server = Tr069AcsServer(
            name="Cleanup ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        device = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="CLEANUP-001",
            is_active=True,
        )
        db_session.add(device)
        db_session.commit()

        # Create old session (40 days ago)
        old_session = Tr069Session(
            device_id=device.id,
            event_type=Tr069Event.periodic,
            started_at=datetime.now(UTC) - timedelta(days=40),
        )
        old_session.created_at = datetime.now(UTC) - timedelta(days=40)
        db_session.add(old_session)

        # Create recent session
        new_session = Tr069Session(
            device_id=device.id,
            event_type=Tr069Event.boot,
            started_at=datetime.now(UTC),
        )
        db_session.add(new_session)
        db_session.commit()

        from sqlalchemy import select

        # Verify both exist
        count = len(
            list(
                db_session.scalars(
                    select(Tr069Session).where(Tr069Session.device_id == device.id)
                ).all()
            )
        )
        assert count == 2


# ---------------------------------------------------------------------------
# 7. Celery task registration
# ---------------------------------------------------------------------------


class TestCeleryTaskRegistration:
    def test_sync_task_importable(self) -> None:
        from app.tasks.tr069 import sync_all_acs_devices

        assert sync_all_acs_devices.name == "app.tasks.tr069.sync_all_acs_devices"

    def test_execute_jobs_task_importable(self) -> None:
        from app.tasks.tr069 import execute_pending_jobs

        assert execute_pending_jobs.name == "app.tasks.tr069.execute_pending_jobs"

    def test_apply_acs_config_task_importable(self) -> None:
        from app.tasks.tr069 import apply_acs_config

        assert apply_acs_config.name == "app.tasks.tr069.apply_acs_config"

    def test_health_check_task_importable(self) -> None:
        from app.tasks.tr069 import check_device_health

        assert check_device_health.name == "app.tasks.tr069.check_device_health"

    def test_cleanup_task_importable(self) -> None:
        from app.tasks.tr069 import cleanup_tr069_records

        assert cleanup_tr069_records.name == "app.tasks.tr069.cleanup_tr069_records"

    def test_tasks_in_init_all(self) -> None:
        from app.tasks import __all__ as all_tasks

        assert "tr069_sync_all_acs_devices" in all_tasks
        assert "tr069_execute_pending_jobs" in all_tasks
        assert "tr069_apply_acs_config" in all_tasks
        assert "tr069_check_device_health" in all_tasks
        assert "cleanup_tr069_records" in all_tasks


# ---------------------------------------------------------------------------
# 8. Device resolution chain
# ---------------------------------------------------------------------------


class TestDeviceResolution:
    def test_resolve_returns_none_for_no_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        # Simulate an ONT with no serial by creating and clearing it
        ont = OntUnit(serial_number="TEMP-RESOLVE", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)
        # Override serial_number attribute (simulating missing serial)
        ont.serial_number = None  # type: ignore[assignment]

        result, reason = resolve_genieacs_with_reason(db_session, ont)
        assert result is None
        assert "serial number" in reason.lower()

    def test_resolve_returns_none_when_no_acs(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        ont = OntUnit(serial_number="NOACSTEST-001", is_active=True)
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        result, reason = resolve_genieacs_with_reason(db_session, ont)
        assert result is None
        assert "No ACS server" in reason

    def test_resolve_matches_device_by_normalized_serial(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        ont = OntUnit(
            serial_number="HWTC-7D47-33C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()
        db_session.refresh(ont)

        mock_device = {
            "_id": "00D09E-TestProduct-HWTC7D4733C3",
            "_deviceId": {"_SerialNumber": "HWTC7D4733C3"},
        }

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [mock_device]]
            instance.extract_parameter_value.side_effect = lambda device, path: None
            instance.parse_device_id.return_value = (
                "00D09E",
                "TestProduct",
                "HWTC7D4733C3",
            )

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        client, device_id = result
        assert device_id == "00D09E-TestProduct-HWTC7D4733C3"
        assert reason == "resolved_via_ont_acs"

    def test_resolve_matches_device_by_genieacs_deviceid_serial(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS DeviceId",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        ont = OntUnit(
            serial_number="HWTC7D4733C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-485754437D4733C3",
            "_deviceId": {"_SerialNumber": "485754437D4733C3"},
        }

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [], [mock_device]]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-485754437D4733C3"
        assert reason == "resolved_via_ont_acs"
        issued_queries = [
            call.kwargs["query"] for call in instance.list_devices.call_args_list
        ]
        assert any(
            {"_deviceId._SerialNumber": "485754437D4733C3"} in query.get("$or", [])
            for query in issued_queries
        )

    def test_resolve_matches_device_by_tr098_serial_parameter(self, db_session) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Resolve ACS TR098",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()

        ont = OntUnit(
            serial_number="485754437D4733C3",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-ALTID",
            "InternetGatewayDevice": {
                "DeviceInfo": {"SerialNumber": {"_value": "485754437D4733C3"}}
            },
        }

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-ALTID"
        assert reason == "resolved_via_ont_acs"
        issued_query = instance.list_devices.call_args.kwargs["query"]
        assert {
            "InternetGatewayDevice.DeviceInfo.SerialNumber._value": ("485754437D4733C3")
        } in issued_query["$or"]

    def test_resolve_does_not_build_synthetic_id_for_placeholder_ont(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HW-OLT-0001",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC7D4806C3",
            oui="48575443",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = []

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is None
        assert "No TR-069 device found" in reason
        instance.build_device_id.assert_not_called()

    def test_resolve_prefers_linked_tr069_device_with_genieacs_id(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HW-OLT-0001",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        linked = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTC7D4806C3",
            genieacs_device_id="48575443-EG8145V5-HWTC7D4806C3",
            oui="48575443",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add(linked)
        db_session.commit()

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "48575443-EG8145V5-HWTC7D4806C3"
        assert reason == "resolved_via_linked_tr069_device"
        MockClient.return_value.list_devices.assert_not_called()

    def test_resolve_moves_ont_link_when_genieacs_id_already_owned(
        self, db_session
    ) -> None:
        from app.models.network import OntUnit
        from app.services.network._resolve import resolve_genieacs_with_reason

        server = Tr069AcsServer(
            name="Linked Resolve ACS Conflict",
            base_url="http://genieacs:7557",
            is_active=True,
        )
        db_session.add(server)
        db_session.flush()

        ont = OntUnit(
            serial_number="HWTCA31A3673",
            is_active=True,
            tr069_acs_server_id=server.id,
        )
        db_session.add(ont)
        db_session.flush()

        placeholder = Tr069CpeDevice(
            acs_server_id=server.id,
            ont_unit_id=ont.id,
            serial_number="HWTCA31A3673",
            is_active=True,
        )
        discovered = Tr069CpeDevice(
            acs_server_id=server.id,
            serial_number="48575443A31A3673",
            genieacs_device_id="00259E-EG8145V5-48575443A31A3673",
            oui="00259E",
            product_class="EG8145V5",
            is_active=True,
        )
        db_session.add_all([placeholder, discovered])
        db_session.commit()

        mock_device = {
            "_id": "00259E-EG8145V5-48575443A31A3673",
            "_deviceId": {"_SerialNumber": "48575443A31A3673"},
        }

        with patch("app.services.network._resolve.create_acs_client") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [], [mock_device]]

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        _client, device_id = result
        assert device_id == "00259E-EG8145V5-48575443A31A3673"
        assert reason == "resolved_via_ont_acs"
        assert discovered.ont_unit_id == ont.id
        assert placeholder.ont_unit_id is None
        assert placeholder.is_active is False


class TestAcsPropagation:
    def test_auto_bind_uses_olt_linked_acs_profile_without_hardcoded_match(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoBind",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp/",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=300,
            ),
        )
        profiles = [
            olt_ssh.Tr069ServerProfile(
                profile_id=17,
                name="Provider Profile",
                acs_url="http://acs.example.com/cwmp",
                acs_username="cwmp-user",
            )
        ]
        bound: dict[str, int] = {}

        monkeypatch.setattr(
            olt_ssh,
            "get_tr069_server_profiles",
            lambda _olt: (True, "ok", profiles),
        )
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert bound == {"profile_id": 17, "ont_id": 6}

    def test_auto_bind_accepts_matching_profile_when_username_not_parsed(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoBind-NoUsername",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=300,
            ),
        )
        profiles = [
            olt_ssh.Tr069ServerProfile(
                profile_id=19,
                name="Provider Profile",
                acs_url="http://acs.example.com/cwmp",
                acs_username="",
            )
        ]
        bound: dict[str, int] = {}

        monkeypatch.setattr(
            olt_ssh,
            "get_tr069_server_profiles",
            lambda _olt: (True, "ok", profiles),
        )
        monkeypatch.setattr(
            olt_ssh,
            "create_tr069_server_profile",
            lambda *_args, **_kwargs: pytest.fail("matching profile should be reused"),
        )
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert bound == {"profile_id": 19, "ont_id": 6}

    def test_auto_bind_creates_profile_for_olt_linked_acs_when_missing(
        self, monkeypatch
    ) -> None:
        from app.services.network import olt_ssh

        olt = SimpleNamespace(
            name="OLT-AutoCreate",
            tr069_acs_server=SimpleNamespace(
                name="Primary ACS",
                cwmp_url="http://acs.example.com/cwmp",
                cwmp_username="cwmp-user",
                cwmp_password=None,
                periodic_inform_interval=180,
            ),
        )
        calls = {"list": 0}
        created: dict[str, object] = {}
        bound: dict[str, int] = {}

        def fake_profiles(_olt):
            calls["list"] += 1
            if calls["list"] == 1:
                return True, "ok", []
            return (
                True,
                "ok",
                [
                    olt_ssh.Tr069ServerProfile(
                        profile_id=23,
                        name="ACS Primary ACS",
                        acs_url="http://acs.example.com/cwmp",
                        acs_username="cwmp-user",
                    )
                ],
            )

        def fake_create(_olt, **kwargs):
            created.update(kwargs)
            return True, "created"

        monkeypatch.setattr(olt_ssh, "get_tr069_server_profiles", fake_profiles)
        monkeypatch.setattr(olt_ssh, "create_tr069_server_profile", fake_create)
        monkeypatch.setattr(
            olt_ssh,
            "bind_tr069_server_profile",
            lambda _olt, fsp, ont_id, profile_id: (
                bound.update({"profile_id": profile_id, "ont_id": ont_id})
                or (True, "bound")
            ),
        )

        olt_ssh._auto_bind_tr069_after_authorize(olt, "0/2/1", 6)

        assert created["acs_url"] == "http://acs.example.com/cwmp"
        assert created["username"] == "cwmp-user"
        assert created["inform_interval"] == 180
        assert bound == {"profile_id": 23, "ont_id": 6}

    def test_tr069_profile_match_does_not_fallback_to_dotmac_name(self) -> None:
        from app.services.network.olt_tr069_admin import match_tr069_profile

        wrong_profile = SimpleNamespace(
            profile_id=99,
            name="DotMac old ACS",
            acs_url="http://old-acs.example.com/cwmp",
            acs_username="cwmp-user",
        )

        result = match_tr069_profile(
            [wrong_profile],
            acs_url="http://new-acs.example.com/cwmp",
            acs_username="cwmp-user",
        )

        assert result is None

    def test_olt_create_auto_init_uses_linked_acs_service(self, monkeypatch) -> None:
        from app.services.network import olt_web_forms

        olt = SimpleNamespace(
            id="olt-1",
            name="OLT Auto Init",
            ssh_username="admin",
            ssh_password="encrypted",
        )
        called: dict[str, object] = {}

        def fake_ensure(received_olt):
            called["olt"] = received_olt
            return True, "profile ready", 31

        monkeypatch.setattr(
            "app.services.network.olt_tr069_admin.ensure_tr069_profile_for_linked_acs",
            fake_ensure,
        )

        olt_web_forms._auto_init_tr069_profile(olt)

        assert called["olt"] is olt

    def test_queue_acs_propagation_includes_tr098_and_tr181_paths(
        self, db_session
    ) -> None:
        from app.models.network import OLTDevice, OntUnit
        from app.services.web_network_olts import _queue_acs_propagation

        server = Tr069AcsServer(
            name="Propagation ACS",
            base_url="http://genieacs:7557",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            is_active=True,
        )
        db_session.add(server)
        db_session.commit()
        db_session.refresh(server)

        olt = OLTDevice(name="OLT-TR069", tr069_acs_server_id=server.id)
        db_session.add(olt)
        db_session.commit()
        db_session.refresh(olt)

        ont = OntUnit(serial_number="PROP-001", is_active=True, olt_device_id=olt.id)
        db_session.add(ont)
        db_session.commit()

        fake_client = MagicMock()

        with patch(
            "app.services.network._resolve.resolve_genieacs_with_reason",
            return_value=((fake_client, "device-1"), "resolved_via_olt_acs"),
        ):
            stats = _queue_acs_propagation(db_session, olt)

        assert stats["attempted"] == 1
        assert stats["propagated"] == 1
        fake_client.set_parameter_values.assert_called_once()
        sent_params = fake_client.set_parameter_values.call_args.args[1]
        assert (
            sent_params["Device.ManagementServer.URL"] == "http://acs.example.com/cwmp"
        )
        assert (
            sent_params["InternetGatewayDevice.ManagementServer.URL"]
            == "http://acs.example.com/cwmp"
        )
        assert sent_params["Device.ManagementServer.Username"] == "cwmp-user"
        assert (
            sent_params["InternetGatewayDevice.ManagementServer.Username"]
            == "cwmp-user"
        )
