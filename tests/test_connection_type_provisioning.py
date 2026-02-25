"""Tests for connection-type-specific provisioning logic."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models.catalog import (
    ConnectionType,
    NasConnectionRule,
    NasDevice,
    NasVendor,
    RadiusProfile,
    Subscription,
)
from app.services.connection_type_provisioning import (
    _mikrotik_commands,
    _rule_matches,
    build_radius_reply_attributes,
    resolve_connection_type,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_subscription():
    sub = MagicMock(spec=Subscription)
    sub.id = uuid4()
    sub.subscriber_id = uuid4()
    sub.offer_id = uuid4()
    sub.radius_profile_id = None
    sub.provisioning_nas_device_id = None
    sub.login = "test_user"
    sub.ipv4_address = "10.0.0.100"
    sub.ipv6_address = None
    sub.mac_address = "AA:BB:CC:DD:EE:FF"
    return sub


@pytest.fixture
def mock_profile():
    profile = MagicMock(spec=RadiusProfile)
    profile.id = uuid4()
    profile.name = "50Mbps-Down"
    profile.code = "50M"
    profile.connection_type = None
    profile.download_speed = 50000
    profile.upload_speed = 25000
    profile.burst_download = None
    profile.burst_upload = None
    profile.burst_threshold = None
    profile.burst_time = None
    profile.ip_pool_name = "pool-residential"
    profile.ipv6_pool_name = None
    profile.session_timeout = 86400
    profile.idle_timeout = 600
    profile.simultaneous_use = 1
    profile.vlan_id = None
    profile.inner_vlan_id = None
    profile.mikrotik_rate_limit = None
    profile.mikrotik_address_list = None
    return profile


@pytest.fixture
def mock_nas_device():
    device = MagicMock(spec=NasDevice)
    device.id = uuid4()
    device.vendor = NasVendor.mikrotik
    device.default_connection_type = ConnectionType.pppoe
    device.supported_connection_types = ["pppoe", "dhcp"]
    return device


# ---------------------------------------------------------------------------
# resolve_connection_type
# ---------------------------------------------------------------------------


class TestResolveConnectionType:
    def test_defaults_to_pppoe(self, mock_subscription):
        db = MagicMock()
        db.get.return_value = None
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.pppoe

    def test_uses_profile_connection_type(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.ipoe
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.ipoe

    def test_uses_nas_default(self, mock_subscription, mock_nas_device):
        db = MagicMock()
        db.get.return_value = mock_nas_device
        mock_nas_device.default_connection_type = ConnectionType.dhcp
        mock_subscription.provisioning_nas_device_id = mock_nas_device.id
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        result = resolve_connection_type(db, mock_subscription)
        assert result == ConnectionType.dhcp


# ---------------------------------------------------------------------------
# _rule_matches
# ---------------------------------------------------------------------------


class TestRuleMatches:
    def test_no_expression_matches_all(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = None
        assert _rule_matches(rule, mock_subscription) is True

    def test_wildcard_matches_all(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_login_prefix_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "login:test_*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_login_prefix_no_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "login:other_*"
        assert _rule_matches(rule, mock_subscription) is False

    def test_mac_prefix_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:AA:BB:*"
        assert _rule_matches(rule, mock_subscription) is True

    def test_mac_exact_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:AA:BB:CC:DD:EE:FF"
        assert _rule_matches(rule, mock_subscription) is True

    def test_mac_no_match(self, mock_subscription):
        rule = MagicMock(spec=NasConnectionRule)
        rule.match_expression = "mac:11:22:*"
        assert _rule_matches(rule, mock_subscription) is False


# ---------------------------------------------------------------------------
# build_radius_reply_attributes
# ---------------------------------------------------------------------------


class TestBuildRadiusReplyAttributes:
    def test_pppoe_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        mock_subscription.radius_profile_id = mock_profile.id
        attrs = build_radius_reply_attributes(db, mock_subscription, profile=mock_profile)
        attr_names = [a["attribute"] for a in attrs]
        assert "Service-Type" in attr_names
        assert "Framed-Protocol" in attr_names
        assert "Framed-Pool" in attr_names
        assert "Session-Timeout" in attr_names
        assert "Framed-IP-Address" in attr_names

    def test_dhcp_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.dhcp
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(db, mock_subscription, profile=mock_profile)
        attr_names = [a["attribute"] for a in attrs]
        assert "Calling-Station-Id" in attr_names
        assert "Framed-Protocol" not in attr_names

    def test_ipoe_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.ipoe
        mock_profile.vlan_id = 100
        mock_profile.inner_vlan_id = None
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        attrs = build_radius_reply_attributes(db, mock_subscription, profile=mock_profile)
        attr_names = [a["attribute"] for a in attrs]
        assert "NAS-Port-Type" in attr_names
        assert "Tunnel-Type" in attr_names
        assert "Tunnel-Private-Group-Id" in attr_names

    def test_hotspot_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.hotspot
        mock_subscription.radius_profile_id = mock_profile.id
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(db, mock_subscription, profile=mock_profile)
        attr_names = [a["attribute"] for a in attrs]
        svc_type = next(a for a in attrs if a["attribute"] == "Service-Type")
        assert svc_type["value"] == "Login-User"
        assert "Mikrotik-Group" in attr_names

    def test_static_attributes(self, mock_subscription, mock_profile):
        db = MagicMock()
        mock_profile.connection_type = ConnectionType.static
        mock_subscription.radius_profile_id = mock_profile.id
        mock_subscription.ipv6_address = "2001:db8::1"
        db.get.return_value = mock_profile
        db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
        db.query.return_value.filter.return_value.all.return_value = []
        attrs = build_radius_reply_attributes(db, mock_subscription, profile=mock_profile)
        attr_names = [a["attribute"] for a in attrs]
        assert "Framed-IP-Address" in attr_names
        assert "Framed-IPv6-Prefix" in attr_names


# ---------------------------------------------------------------------------
# _mikrotik_commands
# ---------------------------------------------------------------------------


class TestMikrotikCommands:
    def test_pppoe_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.pppoe, "create")
        assert len(cmds) == 1
        assert '/ppp secret add' in cmds[0]
        assert 'service=pppoe' in cmds[0]
        assert mock_profile.name in cmds[0]

    def test_pppoe_delete(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.pppoe, "delete")
        assert len(cmds) == 1
        assert '/ppp secret remove' in cmds[0]

    def test_pppoe_suspend(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.pppoe, "suspend")
        assert len(cmds) == 2
        assert 'disabled=yes' in cmds[0]
        assert '/ppp active remove' in cmds[1]

    def test_dhcp_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.dhcp, "create")
        assert len(cmds) == 1
        assert '/ip dhcp-server lease add' in cmds[0]
        assert 'mac-address=' in cmds[0]

    def test_dhcp_delete(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.dhcp, "delete")
        assert len(cmds) == 1
        assert '/ip dhcp-server lease remove' in cmds[0]

    def test_hotspot_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.hotspot, "create")
        assert len(cmds) == 1
        assert '/ip hotspot user add' in cmds[0]
        assert mock_profile.name in cmds[0]

    def test_hotspot_suspend(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.hotspot, "suspend")
        assert len(cmds) == 2
        assert 'disabled=yes' in cmds[0]
        assert '/ip hotspot active remove' in cmds[1]

    def test_static_suspend(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.static, "suspend")
        assert len(cmds) == 1
        assert 'blocked-subscribers' in cmds[0]

    def test_ipoe_create(self, mock_subscription, mock_profile):
        cmds = _mikrotik_commands(mock_subscription, mock_profile, ConnectionType.ipoe, "create")
        assert len(cmds) == 1
        assert '/ip dhcp-server lease add' in cmds[0]
        assert 'use-src-mac=yes' in cmds[0]
