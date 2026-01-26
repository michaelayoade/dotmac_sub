"""Tests for genieacs service."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.genieacs import GenieACSClient, GenieACSError


@pytest.fixture
def client():
    """Create GenieACS client."""
    return GenieACSClient("http://genieacs:7557", timeout=10.0)


@pytest.fixture
def mock_response():
    """Create a mock HTTP response."""
    def _create(status_code=200, json_data=None, text="", headers=None):
        response = MagicMock(spec=httpx.Response)
        response.status_code = status_code
        response.text = text if text else (json.dumps(json_data) if json_data else "")
        response.json.return_value = json_data or {}
        response.headers = headers or {}
        response.raise_for_status.return_value = None
        return response
    return _create


# =============================================================================
# Client Initialization Tests
# =============================================================================


class TestGenieACSClientInit:
    """Tests for GenieACSClient initialization."""

    def test_initializes_with_base_url(self):
        """Test initializes with base URL."""
        client = GenieACSClient("http://localhost:7557")
        assert client.base_url == "http://localhost:7557"

    def test_strips_trailing_slash(self):
        """Test strips trailing slash from base URL."""
        client = GenieACSClient("http://localhost:7557/")
        assert client.base_url == "http://localhost:7557"

    def test_sets_default_timeout(self):
        """Test sets default timeout."""
        client = GenieACSClient("http://localhost:7557")
        assert client.timeout == 30.0

    def test_accepts_custom_timeout(self):
        """Test accepts custom timeout."""
        client = GenieACSClient("http://localhost:7557", timeout=60.0)
        assert client.timeout == 60.0


# =============================================================================
# Request Error Handling Tests
# =============================================================================


class TestRequestErrorHandling:
    """Tests for _request error handling."""

    def test_raises_on_http_error(self, client):
        """Test raises GenieACSError on HTTP error."""
        with patch("httpx.Client") as mock_client:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.text = "Not found"
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Not found", request=MagicMock(), response=mock_response
            )
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response

            with pytest.raises(GenieACSError) as exc_info:
                client._request("GET", "/devices/test")

            assert "API error: 404" in str(exc_info.value)

    def test_raises_on_connection_error(self, client):
        """Test raises GenieACSError on connection error."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.side_effect = (
                httpx.RequestError("Connection refused")
            )

            with pytest.raises(GenieACSError) as exc_info:
                client._request("GET", "/devices")

            assert "Request error" in str(exc_info.value)


# =============================================================================
# Device Operations Tests
# =============================================================================


class TestDeviceOperations:
    """Tests for device operations."""

    def test_list_devices(self, client, mock_response):
        """Test list_devices returns devices."""
        devices = [{"_id": "device1"}, {"_id": "device2"}]

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=devices
            )

            result = client.list_devices()

            assert result == devices
            mock_client.return_value.__enter__.return_value.request.assert_called_once()

    def test_list_devices_with_query(self, client, mock_response):
        """Test list_devices with query filter."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=[]
            )

            client.list_devices(query={"_tags": "test"})

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            assert "params" in call_args.kwargs
            assert "query" in call_args.kwargs["params"]

    def test_list_devices_with_projection(self, client, mock_response):
        """Test list_devices with projection."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=[]
            )

            client.list_devices(projection={"_id": 1})

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            assert "params" in call_args.kwargs
            assert "projection" in call_args.kwargs["params"]

    def test_get_device(self, client, mock_response):
        """Test get_device returns device."""
        device = {"_id": "ABC-Model-123"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=device
            )

            result = client.get_device("ABC-Model-123")

            assert result == device

    def test_delete_device(self, client, mock_response):
        """Test delete_device succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.delete_device("ABC-Model-123")

    def test_count_devices(self, client, mock_response):
        """Test count_devices returns count."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                headers={"X-Total-Count": "42"}
            )

            result = client.count_devices()

            assert result == 42

    def test_count_devices_with_query(self, client, mock_response):
        """Test count_devices with query."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                headers={"X-Total-Count": "10"}
            )

            result = client.count_devices(query={"_tags": "test"})

            assert result == 10

    def test_count_devices_missing_header(self, client, mock_response):
        """Test count_devices returns 0 if header missing."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            result = client.count_devices()

            assert result == 0


# =============================================================================
# Task Operations Tests
# =============================================================================


class TestTaskOperations:
    """Tests for task operations."""

    def test_create_task(self, client, mock_response):
        """Test create_task returns result."""
        task_result = {"_id": "task123"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=task_result, text=json.dumps(task_result)
            )

            result = client.create_task("device1", {"name": "reboot"})

            assert result == task_result

    def test_create_task_empty_response(self, client, mock_response):
        """Test create_task handles empty response."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                text=""
            )

            result = client.create_task("device1", {"name": "reboot"})

            assert result == {}

    def test_create_task_with_connection_request_false(self, client, mock_response):
        """Test create_task with connection_request=False."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.create_task("device1", {"name": "reboot"}, connection_request=False)

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            assert call_args.kwargs["params"]["connection_request"] == "false"

    def test_get_parameter_values(self, client, mock_response):
        """Test get_parameter_values creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.get_parameter_values("device1", ["Device.DeviceInfo.SerialNumber"])

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "getParameterValues"
            assert "parameterNames" in task

    def test_set_parameter_values(self, client, mock_response):
        """Test set_parameter_values creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.set_parameter_values("device1", {"Device.WiFi.SSID": "TestNetwork"})

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "setParameterValues"
            assert "parameterValues" in task

    def test_refresh_object(self, client, mock_response):
        """Test refresh_object creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.refresh_object("device1", "Device.WiFi.")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "refreshObject"
            assert task["objectName"] == "Device.WiFi."

    def test_reboot_device(self, client, mock_response):
        """Test reboot_device creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.reboot_device("device1")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "reboot"

    def test_factory_reset(self, client, mock_response):
        """Test factory_reset creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.factory_reset("device1")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "factoryReset"

    def test_download(self, client, mock_response):
        """Test download creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.download("device1", "1 Firmware Upgrade Image", "http://example.com/fw.bin")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "download"
            assert task["fileType"] == "1 Firmware Upgrade Image"
            assert task["url"] == "http://example.com/fw.bin"

    def test_download_with_filename(self, client, mock_response):
        """Test download with filename creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.download(
                "device1", "1 Firmware Upgrade Image", "http://example.com/fw.bin", filename="fw.bin"
            )

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["filename"] == "fw.bin"

    def test_add_object(self, client, mock_response):
        """Test add_object creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.add_object("device1", "Device.NAT.PortMapping.")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "addObject"
            assert task["objectName"] == "Device.NAT.PortMapping."

    def test_delete_object(self, client, mock_response):
        """Test delete_object creates correct task."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            client.delete_object("device1", "Device.NAT.PortMapping.1.")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            task = call_args.kwargs["json"]
            assert task["name"] == "deleteObject"
            assert task["objectName"] == "Device.NAT.PortMapping.1."

    def test_get_pending_tasks(self, client, mock_response):
        """Test get_pending_tasks returns tasks."""
        tasks = [{"_id": "task1"}, {"_id": "task2"}]

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=tasks
            )

            result = client.get_pending_tasks("device1")

            assert result == tasks

    def test_delete_task(self, client, mock_response):
        """Test delete_task succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.delete_task("task123")


# =============================================================================
# Preset Operations Tests
# =============================================================================


class TestPresetOperations:
    """Tests for preset operations."""

    def test_list_presets(self, client, mock_response):
        """Test list_presets returns presets."""
        presets = [{"_id": "preset1"}, {"_id": "preset2"}]

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=presets
            )

            result = client.list_presets()

            assert result == presets

    def test_get_preset(self, client, mock_response):
        """Test get_preset returns preset."""
        preset = {"_id": "preset1", "channel": "default"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=preset
            )

            result = client.get_preset("preset1")

            assert result == preset

    def test_create_preset(self, client, mock_response):
        """Test create_preset returns preset."""
        preset = {"_id": "new_preset", "channel": "bootstrap"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=preset, text=json.dumps(preset)
            )

            result = client.create_preset(preset)

            assert result == preset

    def test_create_preset_empty_response(self, client, mock_response):
        """Test create_preset handles empty response."""
        preset = {"_id": "new_preset", "channel": "bootstrap"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                text=""
            )

            result = client.create_preset(preset)

            assert result == preset

    def test_delete_preset(self, client, mock_response):
        """Test delete_preset succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.delete_preset("preset1")


# =============================================================================
# Provision Operations Tests
# =============================================================================


class TestProvisionOperations:
    """Tests for provision operations."""

    def test_list_provisions(self, client, mock_response):
        """Test list_provisions returns provisions."""
        provisions = [{"_id": "prov1"}, {"_id": "prov2"}]

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=provisions
            )

            result = client.list_provisions()

            assert result == provisions

    def test_get_provision(self, client, mock_response):
        """Test get_provision returns provision."""
        provision = {"_id": "prov1", "script": "const now = Date.now();"}

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=provision
            )

            result = client.get_provision("prov1")

            assert result == provision

    def test_create_provision(self, client, mock_response):
        """Test create_provision succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.create_provision("prov1", "const now = Date.now();")

    def test_delete_provision(self, client, mock_response):
        """Test delete_provision succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.delete_provision("prov1")


# =============================================================================
# Fault Operations Tests
# =============================================================================


class TestFaultOperations:
    """Tests for fault operations."""

    def test_list_faults(self, client, mock_response):
        """Test list_faults returns faults."""
        faults = [{"_id": "fault1"}, {"_id": "fault2"}]

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=faults
            )

            result = client.list_faults()

            assert result == faults

    def test_list_faults_with_device_id(self, client, mock_response):
        """Test list_faults with device filter."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response(
                json_data=[]
            )

            client.list_faults(device_id="device1")

            call_args = mock_client.return_value.__enter__.return_value.request.call_args
            assert "params" in call_args.kwargs
            assert "query" in call_args.kwargs["params"]

    def test_delete_fault(self, client, mock_response):
        """Test delete_fault succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.delete_fault("fault1")

    def test_retry_fault(self, client, mock_response):
        """Test retry_fault succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.retry_fault("fault1")


# =============================================================================
# Tag Operations Tests
# =============================================================================


class TestTagOperations:
    """Tests for tag operations."""

    def test_add_tag(self, client, mock_response):
        """Test add_tag succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.add_tag("device1", "production")

    def test_remove_tag(self, client, mock_response):
        """Test remove_tag succeeds."""
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.request.return_value = mock_response()

            # Should not raise
            client.remove_tag("device1", "production")


# =============================================================================
# Helper Method Tests
# =============================================================================


class TestHelperMethods:
    """Tests for helper methods."""

    def test_build_device_id(self, client):
        """Test build_device_id creates correct format."""
        device_id = client.build_device_id("001122", "RouterX", "SN123456")
        assert device_id == "001122-RouterX-SN123456"

    def test_parse_device_id(self, client):
        """Test parse_device_id extracts components."""
        oui, product_class, serial = client.parse_device_id("001122-RouterX-SN123456")
        assert oui == "001122"
        assert product_class == "RouterX"
        assert serial == "SN123456"

    def test_parse_device_id_invalid_format(self, client):
        """Test parse_device_id raises on invalid format."""
        with pytest.raises(ValueError) as exc_info:
            client.parse_device_id("invalid")

        assert "Invalid device ID format" in str(exc_info.value)

    def test_extract_parameter_value_simple(self, client):
        """Test extract_parameter_value for simple value."""
        device = {
            "Device": {
                "DeviceInfo": {
                    "SerialNumber": {"_value": "SN123"}
                }
            }
        }
        value = client.extract_parameter_value(device, "Device.DeviceInfo.SerialNumber")
        assert value == "SN123"

    def test_extract_parameter_value_not_found(self, client):
        """Test extract_parameter_value returns None if not found."""
        device = {"Device": {}}
        value = client.extract_parameter_value(device, "Device.DeviceInfo.SerialNumber")
        assert value is None

    def test_extract_parameter_value_no_value_field(self, client):
        """Test extract_parameter_value returns dict if no _value field."""
        device = {
            "Device": {
                "DeviceInfo": {
                    "Manufacturer": "TestCorp"
                }
            }
        }
        value = client.extract_parameter_value(device, "Device.DeviceInfo.Manufacturer")
        assert value == "TestCorp"

    def test_extract_parameter_value_non_dict_intermediate(self, client):
        """Test extract_parameter_value returns None for non-dict intermediate."""
        device = {
            "Device": "not_a_dict"
        }
        value = client.extract_parameter_value(device, "Device.DeviceInfo.SerialNumber")
        assert value is None
