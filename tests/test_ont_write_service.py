"""Tests for ONT write service (Phase 5)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.network.ont_write import OntWriteService

FAKE_UUID = str(uuid.uuid4())
FAKE_UUID2 = str(uuid.uuid4())


class TestUpdateSpeedProfile:
    """Speed profile update tests."""

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_speed_profile(
            db, "missing-id", download_profile_id=FAKE_UUID
        )
        assert result.success is False
        assert "not found" in result.message.lower()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_download_profile(self, mock_get, mock_emit):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_speed_profile(
            db, "ont-1", download_profile_id=FAKE_UUID
        )
        assert result.success is True
        db.commit.assert_called()


class TestUpdateExternalId:
    """External ID update tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_updates_external_id(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_external_id(db, "ont-1", external_id="42")
        assert result.success is True
        assert ont.external_id == "42"
        db.commit.assert_called()

    def test_ont_not_found(self):
        db = MagicMock()
        db.get.return_value = None
        result = OntWriteService.update_external_id(db, "missing", external_id="42")
        assert result.success is False


class TestUpdateWanConfig:
    """WAN config update tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_invalid_wan_mode(self, mock_get):
        ont = MagicMock(wan_mode=None)
        mock_get.return_value = (ont, None)
        db = MagicMock()
        result = OntWriteService.update_wan_config(db, "ont-1", wan_mode="invalid_mode")
        assert result.success is False
        assert "invalid" in result.message.lower()


class TestUpdateManagementIp:
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_vlan_get,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="huawei:4194320640.5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=FAKE_UUID,
        )

        assert result.success is True
        mock_adapter.configure_iphost.assert_called_once()
        # Verify ont_id is passed correctly
        assert mock_adapter.configure_iphost.call_args.args[1] == 5

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_uses_scanned_board_port_context(
        self,
        mock_vlan_get,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=FAKE_UUID,
        )

        assert result.success is True
        # Verify fsp and ont_id are passed correctly
        assert mock_adapter.configure_iphost.call_args.args[0] == "0/1/3"
        assert mock_adapter.configure_iphost.call_args.args[1] == 5

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_accepts_management_vlan_tag_without_vlan_row(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(
                olt=olt,
                fsp="0/1/3",
                ont_id_on_olt=5,
            ),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()
        db.scalars.return_value.first.return_value = None

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_tag=201,
        )

        assert result.success is True
        # Verify vlan is passed correctly in kwargs
        assert mock_adapter.configure_iphost.call_args.kwargs["vlan"] == 201
        assert "mgmt_vlan_id" not in ont.__dict__


class TestUpdateServicePort:
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="huawei:4194320640.5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={"port_index": 700}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        mock_adapter.create_service_port.assert_called_once()
        # Verify ont_id is passed correctly
        assert mock_adapter.create_service_port.call_args.args[1] == 5

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_uses_scanned_board_port_context_and_generic_external_id(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock()
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={"port_index": 700}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        # Verify fsp and ont_id are passed correctly
        assert mock_adapter.create_service_port.call_args.args[0] == "0/1/3"
        assert mock_adapter.create_service_port.call_args.args[1] == 5


class TestOntOltWriteContext:
    def test_requires_scanned_board_port_not_pon_name_fallback(self, db_session):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        olt = OLTDevice(name="Strict Context OLT", vendor="Huawei")
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="0/1/3")
        ont = OntUnit(serial_number="STRICT-CONTEXT-1", external_id="5")
        db_session.add_all([pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        ctx, message = resolve_ont_olt_write_context(db_session, str(ont.id))

        assert ctx is None
        assert message is not None
        assert "scanned board/port" in message

    def test_resolves_from_scanned_board_port_and_ont_id(self, db_session):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        olt = OLTDevice(name="Strict Context OLT", vendor="Huawei")
        db_session.add(olt)
        db_session.flush()

        pon = PonPort(olt_id=olt.id, name="display-only-name")
        ont = OntUnit(
            serial_number="STRICT-CONTEXT-2",
            board="0/1",
            port="3",
            external_id="huawei:4194320640.5",
        )
        db_session.add_all([pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        ctx, message = resolve_ont_olt_write_context(db_session, str(ont.id))

        assert message is None
        assert ctx is not None
        assert ctx.fsp == "0/1/3"
        assert ctx.ont_id_on_olt == 5
        assert ctx.olt.id == olt.id


class TestMoveOnt:
    """ONT move tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_target_port_not_found(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        db.get.return_value = None  # port not found
        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)
        assert result.success is False
        assert "not found" in result.message.lower()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_move_updates_ont_olt_device_id(self, mock_get, mock_emit):
        ont = MagicMock()
        ont.id = "ont-1"
        ont.olt_device_id = "old-olt"
        mock_get.return_value = (ont, None)

        target_port = MagicMock()
        target_port.id = "pon-1"
        target_port.olt_id = "new-olt"

        current_assignment = MagicMock()
        current_assignment.subscriber_id = "sub-1"

        scalar_result = MagicMock()
        scalar_result.first.return_value = current_assignment

        db = MagicMock()
        db.get.return_value = target_port
        db.scalars.return_value = scalar_result

        result = OntWriteService.move_ont(db, "ont-1", target_pon_port_id=FAKE_UUID)

        assert result.success is True
        assert ont.olt_device_id == "new-olt"
        db.commit.assert_called_once()

    def test_resolve_olt_context_prefers_assignment_pon_port_olt(self):
        from app.services.network.ont_write import _resolve_olt_context

        assignment_olt = MagicMock()
        assignment = MagicMock()
        assignment.pon_port = MagicMock()
        assignment.pon_port.olt = assignment_olt

        ont = MagicMock()
        ont.id = "ont-1"
        ont.olt_device = MagicMock()

        scalar_result = MagicMock()
        scalar_result.first.return_value = assignment
        db = MagicMock()
        db.scalars.return_value = scalar_result

        olt, resolved_assignment, error = _resolve_olt_context(db, ont)

        assert error is None
        assert resolved_assignment is assignment
        assert olt is assignment_olt
