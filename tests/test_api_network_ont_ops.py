"""Tests for ONT operational API endpoints (Phase 1 + Phases 4-7)."""

from __future__ import annotations

import pytest

from app.schemas.network_ont_ops import (
    OntActionResponse,
    OntBulkActionRequest,
    OntBulkActionResponse,
    OntConnectionRequestCredentials,
    OntEnrichedRead,
    OntProvisionResponse,
    OntWifiSsidRequest,
)
from app.services.network.ont_action_common import ActionResult


class TestOntActionSchemas:
    """Schema validation tests."""

    def test_action_response_success(self):
        resp = OntActionResponse(success=True, message="Done")
        assert resp.success is True
        assert resp.data is None

    def test_action_response_with_data(self):
        resp = OntActionResponse(success=True, message="OK", data={"key": "val"})
        assert resp.data == {"key": "val"}

    def test_wifi_ssid_min_length(self):
        with pytest.raises(Exception):
            OntWifiSsidRequest(ssid="")

    def test_wifi_ssid_max_length(self):
        with pytest.raises(Exception):
            OntWifiSsidRequest(ssid="x" * 33)

    def test_wifi_ssid_valid(self):
        req = OntWifiSsidRequest(ssid="MyNetwork")
        assert req.ssid == "MyNetwork"

    def test_provision_response_dry_run(self):
        resp = OntProvisionResponse(
            success=True, message="Preview", dry_run=True, steps=[], commands_preview=[]
        )
        assert resp.dry_run is True

    def test_bulk_action_request_validation(self):
        req = OntBulkActionRequest(
            ont_ids=["id1", "id2"], action="reboot", params={}
        )
        assert len(req.ont_ids) == 2
        assert req.action == "reboot"

    def test_bulk_action_request_empty_ids(self):
        with pytest.raises(Exception):
            OntBulkActionRequest(ont_ids=[], action="reboot", params={})

    def test_connection_request_credentials_valid(self):
        req = OntConnectionRequestCredentials(
            username="admin", password="secret123"
        )
        assert req.periodic_inform_interval == 300

    def test_connection_request_credentials_interval(self):
        req = OntConnectionRequestCredentials(
            username="admin", password="secret123", periodic_inform_interval=600
        )
        assert req.periodic_inform_interval == 600

    def test_connection_request_credentials_interval_too_low(self):
        with pytest.raises(Exception):
            OntConnectionRequestCredentials(
                username="admin", password="secret123", periodic_inform_interval=10
            )


class TestOntEnrichedReadSchema:
    """OntEnrichedRead schema tests."""

    def test_minimal_enriched_read(self):
        read = OntEnrichedRead(id="00000000-0000-0000-0000-000000000001")
        assert str(read.id) == "00000000-0000-0000-0000-000000000001"
        assert read.capabilities == {}

    def test_full_enriched_read(self):
        read = OntEnrichedRead(
            id="00000000-0000-0000-0000-000000000001",
            serial_number="HWTC-1234",
            vendor="Huawei",
            model="EG8145V5",
            online_status="online",
            signal_quality="good",
            olt_rx_signal_dbm=-20.5,
            capabilities={"wifi": True, "voip": False},
        )
        assert read.signal_quality == "good"
        assert read.capabilities["wifi"] is True


class TestBulkActionResponse:
    """Bulk action response schema tests."""

    def test_bulk_response(self):
        resp = OntBulkActionResponse(task_id="abc-123", message="Queued 5 ONTs")
        assert resp.task_id == "abc-123"


class TestActionResultConversion:
    """Test that ActionResult dataclass works with API helper."""

    def test_success_result(self):
        result = ActionResult(success=True, message="Rebooted")
        assert result.success is True

    def test_failure_result(self):
        result = ActionResult(success=False, message="ONT not found.")
        assert result.success is False

    def test_result_with_data(self):
        result = ActionResult(success=True, message="OK", data={"uptime": 1234})
        assert result.data["uptime"] == 1234


class TestRouterRegistration:
    """Test that ONT ops router is properly registered in main app."""

    def test_ont_ops_routes_exist(self):
        from app.main import app

        paths = [r.path for r in app.routes]
        assert any("/ont-units/{ont_id}/reboot" in p for p in paths)
        assert any("/ont-units/{ont_id}/provision" in p for p in paths)
        assert any("/ont-units/{ont_id}/enriched" in p for p in paths)
        assert any("/ont-units/bulk-action" in p for p in paths)
        assert any("/ont-units/{ont_id}/connection-request" in p for p in paths)
        assert any("/ont-units/{ont_id}/connection-request-credentials" in p for p in paths)
