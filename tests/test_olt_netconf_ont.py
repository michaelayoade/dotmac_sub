"""Tests for NETCONF-based ONT authorization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.network import olt_netconf_ont


@pytest.fixture
def mock_olt():
    """Create a mock OLT device for testing."""
    olt = SimpleNamespace(
        id="test-olt-123",
        name="Test-OLT",
        mgmt_ip="192.168.1.1",
        netconf_enabled=True,
        netconf_port=830,
        ssh_username="admin",
        ssh_password="enc:test_password",
    )
    return olt


@pytest.fixture
def mock_olt_disabled():
    """Create a mock OLT with NETCONF disabled."""
    olt = SimpleNamespace(
        id="test-olt-456",
        name="Test-OLT-NoNetconf",
        mgmt_ip="192.168.1.2",
        netconf_enabled=False,
        netconf_port=830,
        ssh_username="admin",
        ssh_password="enc:test_password",
    )
    return olt


class TestCanAuthorizeViaNetconf:
    def test_returns_false_when_netconf_disabled(self, mock_olt_disabled):
        ok, reason = olt_netconf_ont.can_authorize_via_netconf(mock_olt_disabled)
        assert ok is False
        assert "not enabled" in reason.lower()

    def test_returns_false_when_connection_fails(self, mock_olt):
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (False, "Connection refused", [])
            ok, reason = olt_netconf_ont.can_authorize_via_netconf(mock_olt)
            assert ok is False
            assert "connection failed" in reason.lower()

    def test_returns_false_when_no_gpon_capabilities(self, mock_olt):
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (
                True,
                "Connected",
                ["urn:ietf:params:netconf:base:1.0", "urn:huawei:yang:huawei-system"],
            )
            ok, reason = olt_netconf_ont.can_authorize_via_netconf(mock_olt)
            assert ok is False
            assert "gpon" in reason.lower()

    def test_returns_true_when_gpon_capabilities_present(self, mock_olt):
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (
                True,
                "Connected",
                [
                    "urn:ietf:params:netconf:base:1.0",
                    "urn:huawei:yang:huawei-gpon?module=huawei-gpon&revision=2021-01-01",
                ],
            )
            ok, reason = olt_netconf_ont.can_authorize_via_netconf(mock_olt)
            assert ok is True
            assert "huawei-gpon" in reason.lower()

    def test_handles_exception_gracefully(self, mock_olt):
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.side_effect = Exception("Network error")
            ok, reason = olt_netconf_ont.can_authorize_via_netconf(mock_olt)
            assert ok is False
            assert "failed" in reason.lower()


class TestDiscoverGponNamespace:
    def test_returns_cached_namespace(self, mock_olt):
        # Prime the cache
        olt_netconf_ont._namespace_cache[str(mock_olt.id)] = "urn:huawei:yang:huawei-gpon"
        try:
            result = olt_netconf_ont.discover_gpon_namespace(mock_olt)
            assert result == "urn:huawei:yang:huawei-gpon"
        finally:
            olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))

    def test_discovers_namespace_from_capabilities(self, mock_olt):
        olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (
                True,
                "Connected",
                ["urn:huawei:yang:huawei-gpon?module=huawei-gpon"],
            )
            result = olt_netconf_ont.discover_gpon_namespace(mock_olt)
            assert result == "urn:huawei:yang:huawei-gpon"

    def test_returns_none_when_connection_fails(self, mock_olt):
        olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (False, "Connection failed", [])
            result = olt_netconf_ont.discover_gpon_namespace(mock_olt)
            assert result is None


class TestBuildOntAddXml:
    def test_generates_valid_xml_structure(self):
        xml = olt_netconf_ont._build_ont_add_xml(
            namespace="urn:huawei:yang:huawei-gpon",
            fsp="0/2/1",
            serial="HWTC7D4733C3",
            line_id=1,
            srv_id=9,
        )
        assert "<serial-number>HWTC7D4733C3</serial-number>" in xml
        assert "<ont-lineprofile-id>1</ont-lineprofile-id>" in xml
        assert "<ont-srvprofile-id>9</ont-srvprofile-id>" in xml
        assert "<auth-type>sn-auth</auth-type>" in xml
        assert "<frame-id>0</frame-id>" in xml
        assert "<slot-id>2</slot-id>" in xml
        assert "<port-id>1</port-id>" in xml
        assert 'xmlns="urn:huawei:yang:huawei-gpon"' in xml

    def test_removes_dashes_from_serial(self):
        xml = olt_netconf_ont._build_ont_add_xml(
            namespace="urn:huawei:yang:huawei-gpon",
            fsp="0/2/1",
            serial="HWTC-7D47-33C3",
            line_id=1,
            srv_id=9,
        )
        assert "<serial-number>HWTC7D4733C3</serial-number>" in xml

    def test_uppercases_serial(self):
        xml = olt_netconf_ont._build_ont_add_xml(
            namespace="urn:huawei:yang:huawei-gpon",
            fsp="0/2/1",
            serial="hwtc7d4733c3",
            line_id=1,
            srv_id=9,
        )
        assert "<serial-number>HWTC7D4733C3</serial-number>" in xml


class TestBuildOntDeleteXml:
    def test_generates_valid_delete_xml(self):
        xml = olt_netconf_ont._build_ont_delete_xml(
            namespace="urn:huawei:yang:huawei-gpon",
            fsp="0/2/1",
            ont_id=5,
        )
        assert "<ont-id>5</ont-id>" in xml
        assert 'nc:operation="delete"' in xml
        assert "<frame-id>0</frame-id>" in xml
        assert "<slot-id>2</slot-id>" in xml
        assert "<port-id>1</port-id>" in xml


class TestAuthorizeOnt:
    def test_validates_fsp_format(self, mock_olt):
        ok, msg, ont_id = olt_netconf_ont.authorize_ont(
            mock_olt,
            "invalid-fsp",
            "HWTC7D4733C3",
            line_profile_id=1,
            service_profile_id=9,
        )
        assert ok is False
        assert "invalid" in msg.lower()
        assert ont_id is None

    def test_validates_serial_format(self, mock_olt):
        ok, msg, ont_id = olt_netconf_ont.authorize_ont(
            mock_olt,
            "0/2/1",
            "invalid!@#serial",
            line_profile_id=1,
            service_profile_id=9,
        )
        assert ok is False
        assert "invalid" in msg.lower()
        assert ont_id is None

    def test_returns_error_when_namespace_not_found(self, mock_olt):
        olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))
        with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
            mock_nc.test_connection.return_value = (True, "Connected", [])
            ok, msg, ont_id = olt_netconf_ont.authorize_ont(
                mock_olt,
                "0/2/1",
                "HWTC7D4733C3",
                line_profile_id=1,
                service_profile_id=9,
            )
            assert ok is False
            assert "namespace" in msg.lower()
            assert ont_id is None

    def test_success_returns_none_for_ont_id(self, mock_olt):
        """ONT-ID is populated by post-auth SNMP sync, not NETCONF."""
        olt_netconf_ont._namespace_cache[str(mock_olt.id)] = "urn:huawei:yang:huawei-gpon"
        try:
            with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
                mock_nc.edit_config.return_value = (True, "Configuration applied")
                ok, msg, ont_id = olt_netconf_ont.authorize_ont(
                    mock_olt,
                    "0/2/1",
                    "HWTC7D4733C3",
                    line_profile_id=1,
                    service_profile_id=9,
                )
                assert ok is True
                assert "authorized" in msg.lower()
                # ONT-ID is None - will be populated by post-auth SNMP sync
                assert ont_id is None
        finally:
            olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))

    def test_edit_config_failure_returns_error(self, mock_olt):
        olt_netconf_ont._namespace_cache[str(mock_olt.id)] = "urn:huawei:yang:huawei-gpon"
        try:
            with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
                mock_nc.edit_config.return_value = (False, "OLT rejected configuration")
                ok, msg, ont_id = olt_netconf_ont.authorize_ont(
                    mock_olt,
                    "0/2/1",
                    "HWTC7D4733C3",
                    line_profile_id=1,
                    service_profile_id=9,
                )
                assert ok is False
                assert "failed" in msg.lower()
                assert ont_id is None
        finally:
            olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))


class TestDeauthorizeOnt:
    def test_validates_fsp_format(self, mock_olt):
        ok, msg = olt_netconf_ont.deauthorize_ont(mock_olt, "bad-fsp", 5)
        assert ok is False
        assert "invalid" in msg.lower()

    def test_success_deletes_ont(self, mock_olt):
        olt_netconf_ont._namespace_cache[str(mock_olt.id)] = "urn:huawei:yang:huawei-gpon"
        try:
            with patch.object(olt_netconf_ont, "olt_netconf") as mock_nc:
                mock_nc.edit_config.return_value = (True, "Configuration applied")
                ok, msg = olt_netconf_ont.deauthorize_ont(mock_olt, "0/2/1", 5)
                assert ok is True
                assert "deleted" in msg.lower()
        finally:
            olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))


class TestHandleRpcError:
    def test_maps_data_exists_error(self):
        from ncclient.operations.rpc import RPCError

        exc = MagicMock(spec=RPCError)
        exc.tag = "data-exists"
        type(exc).__str__ = lambda self: "Data already exists"

        ok, msg = olt_netconf_ont._handle_rpc_error(exc, "HWTC7D4733C3")
        assert ok is False
        assert "already registered" in msg.lower()

    def test_maps_access_denied_error(self):
        from ncclient.operations.rpc import RPCError

        exc = MagicMock(spec=RPCError)
        exc.tag = "access-denied"
        type(exc).__str__ = lambda self: "Permission denied"

        ok, msg = olt_netconf_ont._handle_rpc_error(exc, "HWTC7D4733C3")
        assert ok is False
        assert "permission" in msg.lower()

    def test_maps_lock_denied_error(self):
        from ncclient.operations.rpc import RPCError

        exc = MagicMock(spec=RPCError)
        exc.tag = "lock-denied"
        type(exc).__str__ = lambda self: "Lock failed"

        ok, msg = olt_netconf_ont._handle_rpc_error(exc, "HWTC7D4733C3")
        assert ok is False
        assert "lock" in msg.lower()

    def test_returns_generic_error_for_unknown_tag(self):
        from ncclient.operations.rpc import RPCError

        exc = MagicMock(spec=RPCError)
        exc.tag = "unknown-error"
        type(exc).__str__ = lambda self: "Something went wrong"

        ok, msg = olt_netconf_ont._handle_rpc_error(exc, "HWTC7D4733C3")
        assert ok is False
        assert "something went wrong" in msg.lower()


class TestValidation:
    def test_valid_fsp_formats(self):
        valid_fsps = ["0/0/0", "0/2/1", "1/15/128", "99/99/999"]
        for fsp in valid_fsps:
            ok, _ = olt_netconf_ont._validate_fsp(fsp)
            assert ok, f"Expected {fsp} to be valid"

    def test_invalid_fsp_formats(self):
        invalid_fsps = [
            "0/0",
            "0/0/0/0",
            "a/b/c",
            "0/0/a",
            "",
            "0-0-0",
            " 0/0/0",
        ]
        for fsp in invalid_fsps:
            ok, _ = olt_netconf_ont._validate_fsp(fsp)
            assert not ok, f"Expected {fsp} to be invalid"

    def test_valid_serial_formats(self):
        valid_serials = ["HWTC7D4733C3", "HWTC-7D47-33C3", "ABC123", "a-b-c"]
        for serial in valid_serials:
            ok, _ = olt_netconf_ont._validate_serial(serial)
            assert ok, f"Expected {serial} to be valid"

    def test_invalid_serial_formats(self):
        invalid_serials = ["", "serial!@#", "serial with spaces"]
        for serial in invalid_serials:
            ok, _ = olt_netconf_ont._validate_serial(serial)
            assert not ok, f"Expected {serial} to be invalid"


class TestNamespaceCache:
    def test_clear_specific_olt(self, mock_olt):
        olt_netconf_ont._namespace_cache[str(mock_olt.id)] = "test-namespace"
        olt_netconf_ont._namespace_cache["other-olt"] = "other-namespace"

        olt_netconf_ont.clear_namespace_cache(str(mock_olt.id))

        assert str(mock_olt.id) not in olt_netconf_ont._namespace_cache
        assert "other-olt" in olt_netconf_ont._namespace_cache

        # Cleanup
        olt_netconf_ont.clear_namespace_cache()

    def test_clear_all(self, mock_olt):
        olt_netconf_ont._namespace_cache["olt1"] = "ns1"
        olt_netconf_ont._namespace_cache["olt2"] = "ns2"

        olt_netconf_ont.clear_namespace_cache()

        assert len(olt_netconf_ont._namespace_cache) == 0
