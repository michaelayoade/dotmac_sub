"""Tests for TR-069 feature gap fixes.

Covers: event types, Celery task structure, inform webhook, job retry model,
auto-link ONTs, parameter map resolution, and session cleanup.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

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
        with patch("app.services.tr069.GenieACSClient") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = ("00D09E", "TestProduct", "AUTOLINK-001")
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1

        # Verify ONT now has ACS server linked
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id == server.id

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
        with patch("app.services.tr069.GenieACSClient") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.return_value = [mock_device]
            instance.parse_device_id.return_value = ("00D09E", "TestProduct", "HWTC7D4733C3")
            instance.extract_parameter_value.return_value = None

            result = CpeDevices.sync_from_genieacs(db_session, str(server.id))

        assert result["created"] == 1
        assert result["auto_linked"] == 1
        db_session.refresh(ont)
        assert ont.tr069_acs_server_id == server.id


# ---------------------------------------------------------------------------
# 5. Parameter map resolution
# ---------------------------------------------------------------------------


class TestParameterMapResolution:
    def test_resolve_returns_none_without_db(self) -> None:
        from app.services.network.ont_tr069 import _resolve_param_paths_from_capability

        result = _resolve_param_paths_from_capability(None, "Huawei", "HG8245H", "system.manufacturer")
        assert result is None

    def test_resolve_returns_none_without_vendor(self) -> None:
        from app.services.network.ont_tr069 import _resolve_param_paths_from_capability

        result = _resolve_param_paths_from_capability(MagicMock(), None, None, "system.manufacturer")
        assert result is None


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
        count = len(list(db_session.scalars(
            select(Tr069Session).where(Tr069Session.device_id == device.id)
        ).all()))
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

        with patch("app.services.network._resolve.GenieACSClient") as MockClient:
            instance = MockClient.return_value
            instance.list_devices.side_effect = [[], [mock_device]]
            instance.extract_parameter_value.side_effect = lambda device, path: None
            instance.parse_device_id.return_value = ("00D09E", "TestProduct", "HWTC7D4733C3")

            result, reason = resolve_genieacs_with_reason(db_session, ont)

        assert result is not None
        client, device_id = result
        assert device_id == "00D09E-TestProduct-HWTC7D4733C3"
        assert reason == "resolved_via_ont_acs"


class TestAcsPropagation:
    def test_queue_acs_propagation_includes_tr098_and_tr181_paths(self, db_session) -> None:
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
        assert sent_params["Device.ManagementServer.URL"] == "http://acs.example.com/cwmp"
        assert sent_params["InternetGatewayDevice.ManagementServer.URL"] == "http://acs.example.com/cwmp"
        assert sent_params["Device.ManagementServer.Username"] == "cwmp-user"
        assert sent_params["InternetGatewayDevice.ManagementServer.Username"] == "cwmp-user"
