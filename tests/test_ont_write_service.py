"""Tests for ONT write service (Phase 5)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.network.ont_write import OntWriteService
from app.services.network.provisioning_events import provisioning_correlation

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


class TestUpdateManagementIp:
    @patch("app.services.network.ont_write.upsert_ont_config_override")
    @patch("app.services.network.ont_write.is_bundle_managed_ont")
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_bundle_managed_ont_writes_management_intent_to_overrides(
        self,
        mock_get_adapter,
        mock_get,
        mock_resolve_context,
        mock_emit,
        mock_bundle_managed,
        mock_upsert_override,
    ):
        from app.services.network.olt_protocol_adapters import OltOperationResult

        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        mock_bundle_managed.return_value = True
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=True, message="ok", data={}
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=FAKE_UUID,
            mgmt_ip_address="172.16.201.10",
        )

        assert result.success is True
        assert ont.mgmt_ip_mode is None
        assert ont.mgmt_vlan_id is None
        assert ont.mgmt_ip_address is None
        override_fields = [
            call.kwargs["field_name"] for call in mock_upsert_override.call_args_list
        ]
        assert "management.ip_mode" in override_fields
        assert "management.vlan_tag" in override_fields
        assert "management.ip_address" in override_fields
        assert db.commit.called

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
        olt = MagicMock(id=uuid.uuid4())
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
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

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
        olt = MagicMock(id=uuid.uuid4())
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
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

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

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    @patch("app.services.network.cpe.Vlans.get")
    def test_ssh_timeout_does_not_persist_management_ip(
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
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        mock_vlan_get.return_value = MagicMock(tag=203)

        mock_adapter = MagicMock()
        mock_adapter.configure_iphost.return_value = OltOperationResult(
            success=False,
            message="SSH IPHOST configuration failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()
        db.get.return_value = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=olt.id)

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=FAKE_UUID,
        )

        assert result.success is False
        assert "timed out" in result.message
        db.commit.assert_not_called()
        mock_emit.assert_not_called()

    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_rejects_global_management_vlan_id(
        self,
        mock_get,
        mock_resolve_context,
    ):
        ont = MagicMock(external_id="generic:5")
        mock_get.return_value = (ont, None)
        olt = MagicMock(id=uuid.uuid4())
        mock_resolve_context.return_value = (
            SimpleNamespace(olt=olt, fsp="0/1/3", ont_id_on_olt=5),
            None,
        )
        vlan = MagicMock(id=uuid.uuid4(), tag=203, olt_device_id=None)
        db = MagicMock()
        db.get.return_value = vlan

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=str(vlan.id),
        )

        assert result.success is False
        assert "not configured on this ont's olt" in result.message.lower()
        db.commit.assert_not_called()


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

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write.resolve_ont_olt_write_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_ssh_timeout_does_not_persist_service_port(
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
            success=False,
            message="SSH service port creation failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "timed out" in result.message
        db.commit.assert_not_called()
        mock_emit.assert_not_called()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_allocates_index_before_olt_write(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from sqlalchemy import select

        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntUnit,
            PonPort,
            ServicePortAllocation,
        )
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-OK",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=True, message="created", data={}
        )
        mock_adapter.get_service_ports_for_ont.return_value = OltOperationResult(
            success=True,
            message="ok",
            data={
                "service_ports": [
                    SimpleNamespace(
                        index=321,
                        vlan_id=203,
                        gem_index=1,
                        tag_transform="translate",
                    )
                ]
            },
        )
        mock_get_adapter.return_value = mock_adapter

        result = OntWriteService.update_service_port(
            db_session,
            str(ont.id),
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        port_index = mock_adapter.create_service_port.call_args.kwargs["port_index"]
        assert port_index is not None
        allocation = db_session.scalars(select(ServicePortAllocation)).one()
        assert allocation.port_index == port_index
        assert allocation.vlan_id == 203
        assert allocation.gem_index == 1
        assert allocation.is_active is True
        assert allocation.provisioned_at is not None
        assert allocation.released_at is None
        mock_emit.assert_called_once()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_releases_allocation_when_olt_write_fails(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from sqlalchemy import select

        from app.models.network import (
            OLTDevice,
            OntAssignment,
            OntUnit,
            PonPort,
            ServicePortAllocation,
        )
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service Fail OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-FAIL",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.return_value = OltOperationResult(
            success=False,
            message="SSH service port creation failed: timed out",
            data={},
            error_code="TimeoutError",
        )
        mock_get_adapter.return_value = mock_adapter

        result = OntWriteService.update_service_port(
            db_session,
            str(ont.id),
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is False
        assert "timed out" in result.message
        allocation = db_session.scalars(select(ServicePortAllocation)).one()
        assert allocation.is_active is False
        assert allocation.provisioned_at is None
        assert allocation.released_at is not None
        mock_emit.assert_not_called()

    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.olt_protocol_adapters.get_protocol_adapter")
    def test_db_backed_path_replays_cached_result_for_same_correlation_key(
        self,
        mock_get_adapter,
        mock_emit,
        db_session,
    ):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.olt_protocol_adapters import OltOperationResult

        olt = OLTDevice(name="Write Service Replay OLT", vendor="Huawei")
        pon = PonPort(olt=olt, name="0/1/3")
        ont = OntUnit(
            serial_number="WRITE-SP-REPLAY",
            board="0/1",
            port="3",
            external_id="5",
        )
        db_session.add_all([olt, pon, ont])
        db_session.flush()
        db_session.add(
            OntAssignment(ont_unit_id=ont.id, pon_port_id=pon.id, active=True)
        )
        db_session.commit()

        mock_adapter = MagicMock()
        mock_adapter.create_service_port.side_effect = [
            OltOperationResult(success=True, message="created", data={"port_index": 777}),
            OltOperationResult(success=True, message="created", data={"port_index": 778}),
        ]
        mock_adapter.get_service_ports_for_ont.return_value = OltOperationResult(
            success=True,
            message="ok",
            data={
                "service_ports": [
                    SimpleNamespace(
                        index=777,
                        vlan_id=203,
                        gem_index=1,
                        tag_transform="translate",
                    ),
                    SimpleNamespace(
                        index=778,
                        vlan_id=204,
                        gem_index=1,
                        tag_transform="translate",
                    ),
                ]
            },
        )
        mock_get_adapter.return_value = mock_adapter

        with provisioning_correlation("svc-port:replay:1"):
            first = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=203,
                gem_index=1,
            )
            third = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=204,
                gem_index=1,
            )
            second = OntWriteService.update_service_port(
                db_session,
                str(ont.id),
                vlan_id=203,
                gem_index=1,
            )

        assert first.success is True
        assert second.success is True
        assert third.success is True
        assert first.data == second.data
        assert first.data != third.data
        assert mock_adapter.create_service_port.call_count == 2


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

    def test_queries_locked_assignment_instead_of_relationship_cache(self):
        from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
        from app.services.network.ont_olt_context import resolve_ont_olt_write_context

        ont = OntUnit(
            id=uuid.uuid4(),
            serial_number="STRICT-CONTEXT-LOCK",
            board="0/1",
            port="3",
            external_id="huawei:4194320640.5",
        )
        ont.assignments = []
        assignment = OntAssignment(
            ont_unit_id=ont.id,
            pon_port_id=uuid.uuid4(),
            active=True,
        )
        pon = PonPort(id=assignment.pon_port_id, olt_id=uuid.uuid4(), name="0/1/3")
        olt = OLTDevice(id=pon.olt_id, name="Strict Context OLT", vendor="Huawei")

        db = MagicMock()

        def _get(model, value):
            if model is OntUnit:
                return ont
            if model is PonPort:
                return pon
            if model is OLTDevice:
                return olt
            return None

        db.get.side_effect = _get
        db.scalars.return_value.first.return_value = assignment

        ctx, message = resolve_ont_olt_write_context(db, str(ont.id))

        assert message is None
        assert ctx is not None
        assert ctx.assignment is assignment
        statement = db.scalars.call_args.args[0]
        assert getattr(statement, "_for_update_arg", None) is not None


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
