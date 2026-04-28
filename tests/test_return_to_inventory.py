"""Tests for ONT return-to-inventory flow.

Tests the complete flow of returning an ONT to inventory:
- OLT cleanup (service port deletion, deauthorization)
- Database state reset (assignments, service state)
- Event emission and audit logging
- Autofind refresh
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
    OntWanServiceInstance,
    OnuOnlineStatus,
    WanServiceType,
)
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_inventory import (
    reset_ont_service_state,
    return_ont_to_inventory,
)


@pytest.fixture
def sample_olt(db_session):
    """Create a sample OLT for testing."""
    olt = OLTDevice(
        name="Return-Test-OLT",
        mgmt_ip="10.0.0.50",
        is_active=True,
    )
    db_session.add(olt)
    db_session.commit()
    return olt


@pytest.fixture
def sample_ont(db_session, sample_olt):
    """Create a sample ONT assigned to OLT."""
    ont = OntUnit(
        serial_number="HWTC12345678",
        is_active=True,
        olt_device_id=sample_olt.id,
        board="0/1",
        port="2",
        external_id="0/1/2:5",
        provisioning_status=OntProvisioningStatus.provisioned,
        online_status=OnuOnlineStatus.online,
        mac_address="AA:BB:CC:DD:EE:FF",
        observed_wan_ip="192.168.1.100",
        desired_config={"wan_mode": "pppoe", "vlan": 100},
    )
    db_session.add(ont)
    db_session.commit()
    return ont


@pytest.fixture
def sample_assignment(db_session, sample_ont):
    """Create an active assignment for the ONT."""
    assignment = OntAssignment(
        ont_unit_id=sample_ont.id,
        active=True,
        assigned_at=datetime.now(UTC),
    )
    db_session.add(assignment)
    db_session.commit()
    return assignment


@pytest.fixture
def sample_wan_service(db_session, sample_ont):
    """Create a WAN service instance for the ONT."""
    wan_service = OntWanServiceInstance(
        ont_id=sample_ont.id,
        service_type=WanServiceType.internet,
        connection_type="pppoe",
    )
    db_session.add(wan_service)
    db_session.commit()
    return wan_service


class TestResetOntServiceState:
    """Tests for reset_ont_service_state function."""

    def test_clears_desired_config(self, db_session, sample_ont):
        """Test that desired_config is cleared."""
        assert sample_ont.desired_config == {"wan_mode": "pppoe", "vlan": 100}

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.desired_config == {}

    def test_clears_provisioning_status(self, db_session, sample_ont):
        """Test that provisioning_status is reset to unprovisioned."""
        assert sample_ont.provisioning_status == OntProvisioningStatus.provisioned

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.provisioning_status == OntProvisioningStatus.unprovisioned

    def test_clears_network_state(self, db_session, sample_ont):
        """Test that network state fields are cleared."""
        assert sample_ont.mac_address == "AA:BB:CC:DD:EE:FF"
        assert sample_ont.observed_wan_ip == "192.168.1.100"

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.mac_address is None
        assert sample_ont.observed_wan_ip is None
        assert sample_ont.observed_pppoe_status is None
        assert sample_ont.observed_lan_mode is None

    def test_clears_online_status(self, db_session, sample_ont):
        """Test that online status is reset to unknown."""
        assert sample_ont.online_status == OnuOnlineStatus.online

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert sample_ont.online_status == OnuOnlineStatus.unknown
        assert sample_ont.effective_status == OnuOnlineStatus.unknown

    def test_deletes_wan_service_instances(self, db_session, sample_ont, sample_wan_service):
        """Test that WAN service instances are deleted."""
        assert db_session.query(OntWanServiceInstance).filter(
            OntWanServiceInstance.ont_id == sample_ont.id
        ).count() == 1

        reset_ont_service_state(db_session, sample_ont, reason="test")
        db_session.flush()

        assert db_session.query(OntWanServiceInstance).filter(
            OntWanServiceInstance.ont_id == sample_ont.id
        ).count() == 0


class TestReturnOntToInventory:
    """Tests for return_ont_to_inventory function."""

    def test_success_clears_olt_binding(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test successful return clears OLT binding fields."""
        # Mock external dependencies
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.olt_device_id is None
        assert sample_ont.board is None
        assert sample_ont.port is None
        assert sample_ont.external_id is None

    def test_success_closes_assignment(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test successful return closes active assignment."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_assignment)
        assert sample_assignment.active is False

    def test_success_keeps_ont_active(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test that ONT remains active for reuse (not decommissioned)."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.is_active is True

    def test_success_resets_service_state(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test that service state is reset."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(sample_ont)
        assert sample_ont.desired_config == {}
        assert sample_ont.provisioning_status == OntProvisioningStatus.unprovisioned

    def test_success_returns_data_with_previous_olt(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that result data includes previous OLT info."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": True},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        assert result.data is not None
        assert result.data["olt_id"] == str(sample_olt.id)
        assert result.data["serial_number"] == "HWTC12345678"
        assert result.data["autofind_refreshed"] is True
        assert result.data["autofind_rediscovered"] is True

    def test_failure_olt_cleanup_stops_return(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that failure during OLT cleanup stops the return process."""
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=False,
            message="SSH connection failed",
        )

        with patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is False
        assert "cleanup" in result.message.lower() or "ssh" in result.message.lower()

        # ONT state should NOT be changed
        db_session.refresh(sample_ont)
        assert sample_ont.olt_device_id == sample_olt.id
        db_session.refresh(sample_assignment)
        assert sample_assignment.active is True

    def test_deletes_service_ports_on_olt(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that service ports are deleted from OLT."""
        mock_service_port = SimpleNamespace(index=100)
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": [mock_service_port]},
        )
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        mock_adapter.delete_service_port.assert_called_once_with(100)
        mock_adapter.deauthorize_ont.assert_called_once()

    @pytest.mark.skipif(
        not bool(__import__("os").getenv("TEST_DATABASE_URL")),
        reason="Multiple active assignments test requires PostgreSQL (partial index)",
    )
    def test_handles_multiple_assignments(self, db_session, sample_ont, sample_olt):
        """Test that multiple active assignments are all closed.

        Note: This test only works with PostgreSQL due to partial unique index.
        SQLite treats the partial index as a full unique constraint.
        """
        # Create multiple assignments (edge case)
        assignment1 = OntAssignment(
            ont_unit_id=sample_ont.id,
            active=True,
            assigned_at=datetime.now(UTC),
        )
        assignment2 = OntAssignment(
            ont_unit_id=sample_ont.id,
            active=True,
            assigned_at=datetime.now(UTC),
        )
        db_session.add_all([assignment1, assignment2])
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_ont_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(assignment1)
        db_session.refresh(assignment2)
        assert assignment1.active is False
        assert assignment2.active is False


class TestReturnToInventoryWebAction:
    """Tests for the web action version of return_to_inventory."""

    def test_success_emits_events(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test that success emits deauthorization and service port events."""
        from app.services.web_network_ont_actions.inventory import return_to_inventory

        mock_service_port = SimpleNamespace(index=100)
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": [mock_service_port]},
        )
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_actions.inventory.emit_event"
            ) as mock_emit,
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        # Check events were emitted
        assert mock_emit.call_count >= 2  # service port deleted + ont deauthorized

    def test_clears_tr069_binding(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test that TR-069 device binding is cleared."""
        from app.models.tr069 import Tr069AcsServer, Tr069CpeDevice
        from app.services.web_network_ont_actions.inventory import return_to_inventory

        # Create ACS server first (required FK)
        acs_server = Tr069AcsServer(
            name="Test ACS",
            base_url="http://localhost:7557",
        )
        db_session.add(acs_server)
        db_session.flush()

        # Create TR-069 device linked to ONT
        tr069_device = Tr069CpeDevice(
            serial_number="TR069-12345",
            acs_server_id=acs_server.id,
            ont_unit_id=sample_ont.id,
        )
        db_session.add(tr069_device)
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_actions.inventory.emit_event"
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        db_session.refresh(tr069_device)
        assert tr069_device.ont_unit_id is None

    def test_releases_service_port_allocations(
        self, db_session, sample_ont, sample_olt, sample_assignment
    ):
        """Test that service port DB allocations are released."""
        from app.services.web_network_ont_actions.inventory import return_to_inventory

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_actions.inventory.emit_event"
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=2,
            ) as mock_release,
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
        ):
            result = return_to_inventory(db_session, str(sample_ont.id))

        assert result.success is True
        mock_release.assert_called_once_with(db_session, str(sample_ont.id))


class TestReturnToInventoryForWeb:
    """Tests for the route-friendly wrapper."""

    def test_returns_not_found_for_missing_ont(self, db_session):
        """Test that missing ONT returns failure result."""
        from app.services.web_network_ont_actions.inventory import return_to_inventory_for_web

        result = return_to_inventory_for_web(
            db_session,
            "00000000-0000-0000-0000-000000000000",
        )

        assert result.success is False
        assert "not found" in result.message.lower()

    def test_logs_audit_event(self, db_session, sample_ont, sample_olt, sample_assignment):
        """Test that audit event is logged."""
        from app.services.web_network_ont_actions.inventory import return_to_inventory_for_web

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="ONT deauthorized",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.web_network_ont_actions.inventory.emit_event"
            ),
            patch(
                "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
                return_value={"ok": True, "rediscovered": False},
            ),
            patch(
                "app.services.web_network_ont_actions.inventory._log_action_audit"
            ) as mock_audit,
        ):
            result = return_to_inventory_for_web(db_session, str(sample_ont.id))

        assert result.success is True
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "return_to_inventory"
        assert call_kwargs["ont_id"] == sample_ont.id


class TestCleanupOltStateForReturn:
    """Tests for the OLT cleanup helper."""

    def test_returns_success_when_no_olt_context(self, db_session):
        """Test that cleanup succeeds when ONT has no OLT context."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        # Create ONT without OLT binding
        ont = OntUnit(
            serial_number="UNBOUND-ONT",
            is_active=True,
            olt_device_id=None,
            board=None,
            port=None,
        )
        db_session.add(ont)
        db_session.commit()

        success, completed, errors = _cleanup_olt_state_for_return(db_session, str(ont.id))

        assert success is True
        assert errors == []

    def test_failure_when_service_port_read_fails(
        self, db_session, sample_ont, sample_olt
    ):
        """Test that failure to read service ports stops cleanup."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=False,
            message="Connection timeout",
        )

        with patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is False
        assert any("service-ports" in e.lower() for e in errors)

    def test_failure_when_deauthorization_fails(
        self, db_session, sample_ont, sample_olt
    ):
        """Test that deauthorization failure stops cleanup."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": []},
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=False,
            message="ONT not found on OLT",
        )

        with patch(
            "app.services.network.olt_protocol_adapters.get_protocol_adapter",
            return_value=mock_adapter,
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is False
        assert any("deauthorize" in e.lower() for e in errors)

    def test_tracks_completed_steps(self, db_session, sample_ont, sample_olt):
        """Test that completed steps are tracked."""
        from app.services.web_network_ont_actions.inventory import (
            _cleanup_olt_state_for_return,
        )

        mock_service_port = SimpleNamespace(index=42)
        mock_adapter = MagicMock()
        mock_adapter.get_service_ports_for_ont.return_value = ActionResult(
            success=True,
            message="OK",
            data={"service_ports": [mock_service_port]},
        )
        mock_adapter.delete_service_port.return_value = ActionResult(
            success=True,
            message="Deleted",
        )
        mock_adapter.deauthorize_ont.return_value = ActionResult(
            success=True,
            message="OK",
        )

        with (
            patch(
                "app.services.network.olt_protocol_adapters.get_protocol_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "app.services.network.service_port_allocator.release_all_for_ont",
                return_value=1,
            ),
        ):
            success, completed, errors = _cleanup_olt_state_for_return(
                db_session, str(sample_ont.id)
            )

        assert success is True
        assert any("service-port 42" in c.lower() for c in completed)
        assert any("deauthorized" in c.lower() for c in completed)
        assert any("allocation" in c.lower() for c in completed)


class TestOntWithoutOltBinding:
    """Tests for ONTs that have no OLT binding."""

    def test_return_works_without_olt(self, db_session):
        """Test that return works for ONT without OLT binding."""
        # Create ONT without OLT binding (e.g., manually added)
        ont = OntUnit(
            serial_number="STANDALONE-ONT",
            is_active=True,
            olt_device_id=None,
            board=None,
            port=None,
            provisioning_status=OntProvisioningStatus.provisioned,
            desired_config={"some": "config"},
        )
        db_session.add(ont)
        db_session.flush()  # Get ont.id before using in assignment

        assignment = OntAssignment(
            ont_unit_id=ont.id,
            active=True,
        )
        db_session.add(assignment)
        db_session.commit()

        with patch(
            "app.services.web_network_ont_autofind.refresh_returned_ont_autofind",
            return_value={"ok": False, "message": "No OLT"},
        ):
            result = return_ont_to_inventory(db_session, str(ont.id))

        assert result.success is True
        db_session.refresh(ont)
        db_session.refresh(assignment)
        assert ont.desired_config == {}
        assert ont.provisioning_status == OntProvisioningStatus.unprovisioned
        assert assignment.active is False
