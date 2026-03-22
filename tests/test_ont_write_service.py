"""Tests for ONT write service (Phase 5)."""

from __future__ import annotations

import uuid
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
        result = OntWriteService.update_wan_config(
            db, "ont-1", wan_mode="invalid_mode"
        )
        assert result.success is False
        assert "invalid" in result.message.lower()


class TestUpdateManagementIp:
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write._resolve_olt_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_ssh_ont.configure_ont_iphost")
    @patch("app.services.network.cpe.Vlans.get")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_vlan_get,
        mock_configure,
        mock_get,
        mock_resolve,
        mock_emit,
    ):
        ont = MagicMock(external_id="huawei:4194320640.5")
        assignment = MagicMock()
        assignment.pon_port = MagicMock(name="0/1/3")
        assignment.pon_port.name = "0/1/3"
        mock_get.return_value = (ont, None)
        mock_resolve.return_value = (MagicMock(), assignment, None)
        mock_vlan_get.return_value = MagicMock(tag=203)
        mock_configure.return_value = (True, "ok")
        db = MagicMock()

        result = OntWriteService.update_management_ip(
            db,
            "ont-1",
            mgmt_ip_mode="dhcp",
            mgmt_vlan_id=FAKE_UUID,
        )

        assert result.success is True
        mock_configure.assert_called_once()
        assert mock_configure.call_args.args[2] == 5


class TestUpdateServicePort:
    @patch("app.services.network.ont_write._emit_ont_event")
    @patch("app.services.network.ont_write._resolve_olt_context")
    @patch("app.services.network.ont_write.get_ont_or_error")
    @patch("app.services.network.olt_ssh_service_ports.create_single_service_port")
    def test_accepts_huawei_dotted_external_id(
        self,
        mock_create,
        mock_get,
        mock_resolve,
        mock_emit,
    ):
        ont = MagicMock(external_id="huawei:4194320640.5")
        assignment = MagicMock()
        assignment.pon_port = MagicMock(name="0/1/3")
        assignment.pon_port.name = "0/1/3"
        mock_get.return_value = (ont, None)
        mock_resolve.return_value = (MagicMock(), assignment, None)
        mock_create.return_value = (True, "created")
        db = MagicMock()

        result = OntWriteService.update_service_port(
            db,
            "ont-1",
            vlan_id=203,
            gem_index=1,
        )

        assert result.success is True
        mock_create.assert_called_once()
        assert mock_create.call_args.args[2] == 5


class TestMoveOnt:
    """ONT move tests."""

    @patch("app.services.network.ont_write.get_ont_or_error")
    def test_target_port_not_found(self, mock_get):
        ont = MagicMock()
        mock_get.return_value = (ont, None)
        db = MagicMock()
        db.get.return_value = None  # port not found
        result = OntWriteService.move_ont(
            db, "ont-1", target_pon_port_id=FAKE_UUID
        )
        assert result.success is False
        assert "not found" in result.message.lower()
