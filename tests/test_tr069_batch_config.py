"""Tests for TR-069 parameter batching."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.network.tr069_batch_config import (
    TR098_PATHS,
    TR181_PATHS,
    Tr069ConfigBatch,
    submit_batched_config,
)


class TestTr069ConfigBatch:
    """Tests for Tr069ConfigBatch class."""

    def test_empty_batch(self):
        """New batch should be empty."""
        batch = Tr069ConfigBatch()

        assert batch.is_empty is True
        assert batch.parameter_count == 0

    def test_default_data_model(self):
        """Default data model should be TR-181 (Device)."""
        batch = Tr069ConfigBatch()

        assert batch.data_model == "Device"
        assert batch.paths == TR181_PATHS

    def test_tr098_data_model(self):
        """TR-098 data model should use IGD paths."""
        batch = Tr069ConfigBatch(data_model="InternetGatewayDevice")

        assert batch.data_model == "InternetGatewayDevice"
        assert batch.paths == TR098_PATHS

    def test_invalid_data_model(self):
        """Invalid data model should raise error."""
        with pytest.raises(ValueError, match="Invalid data model"):
            Tr069ConfigBatch(data_model="Invalid")

    def test_add_parameter(self):
        """Should add raw parameter to batch."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test.Param", "value")

        assert batch.is_empty is False
        assert batch.parameter_count == 1
        assert batch.parameters["Device.Test.Param"] == "value"


class TestConnectionRequestCredentials:
    """Tests for add_connection_request_credentials method."""

    def test_tr181_paths(self):
        """Should use correct TR-181 paths."""
        batch = Tr069ConfigBatch(data_model="Device")
        batch.add_connection_request_credentials("user", "pass")

        assert "Device.ManagementServer.ConnectionRequestUsername" in batch.parameters
        assert "Device.ManagementServer.ConnectionRequestPassword" in batch.parameters
        assert batch.parameters["Device.ManagementServer.ConnectionRequestUsername"] == "user"
        assert batch.parameters["Device.ManagementServer.ConnectionRequestPassword"] == "pass"

    def test_tr098_paths(self):
        """Should use correct TR-098 paths."""
        batch = Tr069ConfigBatch(data_model="InternetGatewayDevice")
        batch.add_connection_request_credentials("user", "pass")

        assert "InternetGatewayDevice.ManagementServer.ConnectionRequestUsername" in batch.parameters
        assert "InternetGatewayDevice.ManagementServer.ConnectionRequestPassword" in batch.parameters

    def test_periodic_inform_defaults(self):
        """Should set default periodic inform settings."""
        batch = Tr069ConfigBatch()
        batch.add_connection_request_credentials("user", "pass")

        assert batch.parameters["Device.ManagementServer.PeriodicInformEnable"] is True
        assert batch.parameters["Device.ManagementServer.PeriodicInformInterval"] == 300

    def test_custom_periodic_inform(self):
        """Should allow custom periodic inform settings."""
        batch = Tr069ConfigBatch()
        batch.add_connection_request_credentials(
            "user",
            "pass",
            periodic_inform_interval=600,
            periodic_inform_enable=False,
        )

        assert batch.parameters["Device.ManagementServer.PeriodicInformEnable"] is False
        assert batch.parameters["Device.ManagementServer.PeriodicInformInterval"] == 600


class TestPPPoECredentials:
    """Tests for add_pppoe_credentials method."""

    def test_basic_pppoe(self):
        """Should add PPPoE credentials."""
        batch = Tr069ConfigBatch()
        batch.add_pppoe_credentials(
            wan_path="Device.PPP.Interface.1.",
            username="ppp_user",
            password="ppp_pass",
        )

        assert batch.parameters["Device.PPP.Interface.1.Username"] == "ppp_user"
        assert batch.parameters["Device.PPP.Interface.1.Password"] == "ppp_pass"

    def test_path_normalization(self):
        """Should handle paths with or without trailing dot."""
        batch1 = Tr069ConfigBatch()
        batch1.add_pppoe_credentials(
            wan_path="Device.PPP.Interface.1",  # No trailing dot
            username="user",
            password="pass",
        )

        batch2 = Tr069ConfigBatch()
        batch2.add_pppoe_credentials(
            wan_path="Device.PPP.Interface.1.",  # With trailing dot
            username="user",
            password="pass",
        )

        # Both should produce same result
        assert batch1.parameters == batch2.parameters


class TestLANConfig:
    """Tests for add_lan_config method."""

    def test_full_lan_config(self):
        """Should add full LAN configuration."""
        batch = Tr069ConfigBatch()
        batch.add_lan_config(
            lan_ip="192.168.1.1",
            subnet="255.255.255.0",
            dhcp_enabled=True,
            dhcp_start="192.168.1.100",
            dhcp_end="192.168.1.200",
        )

        assert batch.parameter_count == 5
        assert "192.168.1.1" in batch.parameters.values()
        assert "255.255.255.0" in batch.parameters.values()
        assert True in batch.parameters.values()

    def test_partial_lan_config(self):
        """Should only add provided parameters."""
        batch = Tr069ConfigBatch()
        batch.add_lan_config(lan_ip="192.168.1.1")

        assert batch.parameter_count == 1
        paths = batch.paths
        assert batch.parameters[paths["lan_ip"]] == "192.168.1.1"

    def test_empty_lan_config(self):
        """Should not add anything if all None."""
        batch = Tr069ConfigBatch()
        batch.add_lan_config()

        assert batch.is_empty is True


class TestWiFiConfig:
    """Tests for add_wifi_config method."""

    def test_basic_wifi_config(self):
        """Should add basic WiFi configuration."""
        batch = Tr069ConfigBatch()
        batch.add_wifi_config(
            ssid="MyNetwork",
            password="secret123",
            enabled=True,
        )

        assert batch.parameter_count == 3

    def test_tr181_wifi_paths(self):
        """Should use TR-181 WiFi paths."""
        batch = Tr069ConfigBatch(data_model="Device")
        batch.add_wifi_config(ssid="TestSSID")

        # TR-181 uses Device.WiFi.SSID.1.SSID
        assert "Device.WiFi.SSID.1.SSID" in batch.parameters

    def test_tr098_wifi_paths(self):
        """Should use TR-098 WiFi paths."""
        batch = Tr069ConfigBatch(data_model="InternetGatewayDevice")
        batch.add_wifi_config(ssid="TestSSID")

        # TR-098 uses InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID
        assert "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID" in batch.parameters

    def test_custom_wifi_path(self):
        """Should allow custom WiFi path."""
        batch = Tr069ConfigBatch()
        batch.add_wifi_config(
            ssid="CustomSSID",
            wifi_path="Device.WiFi.SSID.2.",
        )

        assert "Device.WiFi.SSID.2.SSID" in batch.parameters

    def test_wifi_password_tr181(self):
        """TR-181 should use X_HW_PreSharedKey."""
        batch = Tr069ConfigBatch(data_model="Device")
        batch.add_wifi_config(password="secret")

        assert "Device.WiFi.SSID.1.X_HW_PreSharedKey" in batch.parameters

    def test_wifi_password_tr098(self):
        """TR-098 should use PreSharedKey.1.PreSharedKey."""
        batch = Tr069ConfigBatch(data_model="InternetGatewayDevice")
        batch.add_wifi_config(password="secret")

        path = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey"
        assert path in batch.parameters

    def test_wifi_channel(self):
        """Should add channel configuration."""
        batch = Tr069ConfigBatch(data_model="Device")
        batch.add_wifi_config(channel=6)

        assert "Device.WiFi.Radio.1.Channel" in batch.parameters
        assert batch.parameters["Device.WiFi.Radio.1.Channel"] == 6


class TestSubmit:
    """Tests for submit method."""

    def test_empty_batch_submit(self):
        """Empty batch should return success without API call."""
        batch = Tr069ConfigBatch()
        client = MagicMock()

        success, message, data = batch.submit(client, "device-001")

        assert success is True
        assert "empty" in message.lower()
        client.set_parameter_values.assert_not_called()

    def test_successful_submit(self):
        """Should submit parameters successfully."""
        batch = Tr069ConfigBatch()
        batch.add_connection_request_credentials("user", "pass")

        client = MagicMock()
        client.set_parameter_values.return_value = {"_id": "task-123"}

        success, message, data = batch.submit(client, "device-001")

        assert success is True
        assert data["task_id"] == "task-123"
        assert data["parameter_count"] == 4
        client.set_parameter_values.assert_called_once()

    def test_triggers_inform(self):
        """Should trigger connection request after submit."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")

        client = MagicMock()
        client.set_parameter_values.return_value = {"_id": "task-123"}

        batch.submit(client, "device-001", trigger_inform=True)

        client.send_connection_request.assert_called_once_with("device-001")

    def test_no_inform_when_disabled(self):
        """Should not trigger inform when disabled."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")

        client = MagicMock()
        client.set_parameter_values.return_value = {"_id": "task-123"}

        batch.submit(client, "device-001", trigger_inform=False)

        client.send_connection_request.assert_not_called()

    def test_inform_failure_ignored(self):
        """Connection request failure should not cause submit failure."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")

        client = MagicMock()
        client.set_parameter_values.return_value = {"_id": "task-123"}
        client.send_connection_request.side_effect = Exception("Connection refused")

        success, message, data = batch.submit(client, "device-001")

        assert success is True

    def test_already_pending_task(self):
        """Should handle already pending task response."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")

        client = MagicMock()
        client.set_parameter_values.return_value = {
            "_id": "task-123",
            "alreadyPending": True,
        }

        success, message, data = batch.submit(client, "device-001")

        assert success is True
        assert data["merged"] is True
        assert "merged" in message.lower()

    def test_api_failure(self):
        """Should handle API failure."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")

        client = MagicMock()
        client.set_parameter_values.side_effect = Exception("API error")

        success, message, data = batch.submit(client, "device-001")

        assert success is False
        assert "error" in data


class TestFromOntConfig:
    """Tests for from_ont_config class method."""

    def test_basic_creation(self):
        """Should create batch from ONT config."""
        ont = MagicMock()
        ont.tr069_data_model = "Device"
        ont.lan_gateway_ip = None
        ont.lan_subnet_mask = None
        ont.lan_dhcp_enabled = None
        ont.lan_dhcp_start = None
        ont.lan_dhcp_end = None
        ont.wifi_ssid = None
        ont.wifi_password = None
        ont.wifi_enabled = None
        ont.wifi_channel = None

        profile = MagicMock()
        profile.cr_username = "cr_user"
        profile.cr_password = "cr_pass"

        batch = Tr069ConfigBatch.from_ont_config(ont, profile)

        assert batch.data_model == "Device"
        assert not batch.is_empty

    def test_fallback_data_model(self):
        """Should fallback to Device when data model not set."""
        ont = MagicMock()
        ont.tr069_data_model = None
        ont.lan_gateway_ip = None
        ont.lan_subnet_mask = None
        ont.lan_dhcp_enabled = None
        ont.lan_dhcp_start = None
        ont.lan_dhcp_end = None
        ont.wifi_ssid = None
        ont.wifi_password = None
        ont.wifi_enabled = None
        ont.wifi_channel = None

        profile = MagicMock()
        profile.cr_username = "user"
        profile.cr_password = "pass"

        batch = Tr069ConfigBatch.from_ont_config(ont, profile)

        assert batch.data_model == "Device"

    def test_includes_lan_config(self):
        """Should include LAN config from ONT."""
        ont = MagicMock()
        ont.tr069_data_model = "Device"
        ont.lan_gateway_ip = "192.168.1.1"
        ont.lan_subnet_mask = "255.255.255.0"
        ont.lan_dhcp_enabled = True
        ont.lan_dhcp_start = "192.168.1.100"
        ont.lan_dhcp_end = "192.168.1.200"
        ont.wifi_ssid = None
        ont.wifi_password = None
        ont.wifi_enabled = None
        ont.wifi_channel = None

        profile = MagicMock()
        profile.cr_username = None
        profile.cr_password = None

        batch = Tr069ConfigBatch.from_ont_config(ont, profile)

        paths = batch.paths
        assert batch.parameters[paths["lan_ip"]] == "192.168.1.1"

    def test_includes_wifi_config(self):
        """Should include WiFi config from ONT."""
        ont = MagicMock()
        ont.tr069_data_model = "Device"
        ont.lan_gateway_ip = None
        ont.lan_subnet_mask = None
        ont.lan_dhcp_enabled = None
        ont.lan_dhcp_start = None
        ont.lan_dhcp_end = None
        ont.wifi_ssid = "TestNetwork"
        ont.wifi_password = "secret"
        ont.wifi_enabled = True
        ont.wifi_channel = 11

        profile = MagicMock()
        profile.cr_username = None
        profile.cr_password = None

        batch = Tr069ConfigBatch.from_ont_config(ont, profile)

        assert "Device.WiFi.SSID.1.SSID" in batch.parameters
        assert batch.parameters["Device.WiFi.SSID.1.SSID"] == "TestNetwork"

    def test_explicit_credentials_override(self):
        """Explicit credentials should override profile."""
        ont = MagicMock()
        ont.tr069_data_model = "Device"
        ont.lan_gateway_ip = None
        ont.lan_subnet_mask = None
        ont.lan_dhcp_enabled = None
        ont.lan_dhcp_start = None
        ont.lan_dhcp_end = None
        ont.wifi_ssid = None
        ont.wifi_password = None
        ont.wifi_enabled = None
        ont.wifi_channel = None

        profile = MagicMock()
        profile.cr_username = "profile_user"
        profile.cr_password = "profile_pass"

        batch = Tr069ConfigBatch.from_ont_config(
            ont,
            profile,
            cr_username="explicit_user",
            cr_password="explicit_pass",
        )

        paths = batch.paths
        assert batch.parameters[paths["cr_username"]] == "explicit_user"
        assert batch.parameters[paths["cr_password"]] == "explicit_pass"


class TestSubmitBatchedConfig:
    """Tests for submit_batched_config function."""

    @patch("app.services.network.ont_action_common.get_ont_client_or_error")
    def test_empty_batch(self, mock_get_client):
        """Empty batch should return success without API call."""
        batch = Tr069ConfigBatch()
        db = MagicMock()

        success, message, data = submit_batched_config(db, "ont-001", batch)

        assert success is True
        assert "empty" in message.lower()
        mock_get_client.assert_not_called()

    @patch("app.services.network.ont_action_common.get_ont_client_or_error")
    def test_client_resolution_error(self, mock_get_client):
        """Should handle client resolution failure."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")
        db = MagicMock()

        error = MagicMock()
        error.message = "ONT not found"
        mock_get_client.return_value = (None, error)

        success, message, data = submit_batched_config(db, "ont-001", batch)

        assert success is False
        assert "ONT not found" in message

    @patch("app.services.network.ont_action_common.get_ont_client_or_error")
    def test_successful_submit(self, mock_get_client):
        """Should submit batch successfully."""
        batch = Tr069ConfigBatch()
        batch.add_parameter("Device.Test", "value")
        db = MagicMock()

        ont = MagicMock()
        client = MagicMock()
        client.set_parameter_values.return_value = {"_id": "task-123"}
        mock_get_client.return_value = ((ont, client, "device-001"), None)

        success, message, data = submit_batched_config(db, "ont-001", batch)

        assert success is True
        assert data["task_id"] == "task-123"
