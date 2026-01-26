"""GenieACS API client for TR-069 device management.

This module provides a client for interacting with the GenieACS NBI
(Northbound Interface) to manage TR-069/CWMP devices.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class GenieACSError(Exception):
    """Base exception for GenieACS client errors."""

    pass


class GenieACSClient:
    """HTTP client for GenieACS NBI (Northbound Interface).

    The GenieACS NBI provides REST API access to manage TR-069 devices,
    create tasks, manage presets, and retrieve device information.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        headers: dict | None = None,
    ):
        """Initialize GenieACS client.

        Args:
            base_url: GenieACS NBI base URL (e.g., http://genieacs:7557)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = headers or {}

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Make HTTP request to GenieACS NBI.

        Args:
            method: HTTP method
            path: API path
            params: Query parameters
            json_data: JSON request body
            **kwargs: Additional httpx arguments

        Returns:
            HTTP response

        Raises:
            GenieACSError: On request failure
        """
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout, headers=self.headers) as client:
                response = client.request(
                    method, url, params=params, json=json_data, **kwargs
                )
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as e:
            logger.error(f"GenieACS API error: {e.response.status_code} - {e.response.text}")
            raise GenieACSError(f"API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"GenieACS request error: {e}")
            raise GenieACSError(f"Request error: {e}") from e

    # -------------------------------------------------------------------------
    # Device Operations
    # -------------------------------------------------------------------------

    def list_devices(self, query: dict | None = None, projection: dict | None = None) -> list[dict]:
        """List devices with optional filtering.

        Args:
            query: MongoDB-style query filter
            projection: Fields to include/exclude

        Returns:
            List of device documents
        """
        params = {}
        if query:
            params["query"] = json.dumps(query)
        if projection:
            params["projection"] = json.dumps(projection)

        response = self._request("GET", "/devices", params=params)
        return response.json()

    def get_device(self, device_id: str) -> dict:
        """Get device by ID.

        Args:
            device_id: Device ID (format: OUI-ProductClass-SerialNumber)

        Returns:
            Device document
        """
        encoded_id = quote(device_id, safe="")
        response = self._request("GET", f"/devices/{encoded_id}")
        return response.json()

    def delete_device(self, device_id: str) -> None:
        """Delete device.

        Args:
            device_id: Device ID
        """
        encoded_id = quote(device_id, safe="")
        self._request("DELETE", f"/devices/{encoded_id}")

    def count_devices(self, query: dict | None = None) -> int:
        """Count devices matching query.

        Args:
            query: MongoDB-style query filter

        Returns:
            Device count
        """
        params = {}
        if query:
            params["query"] = json.dumps(query)

        response = self._request("HEAD", "/devices", params=params)
        return int(response.headers.get("X-Total-Count", 0))

    # -------------------------------------------------------------------------
    # Task Operations
    # -------------------------------------------------------------------------

    def create_task(
        self,
        device_id: str,
        task: dict,
        connection_request: bool = True,
    ) -> dict:
        """Create a task for a device.

        Args:
            device_id: Device ID
            task: Task definition
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        encoded_id = quote(device_id, safe="")
        params = {"connection_request": str(connection_request).lower()}

        response = self._request(
            "POST",
            f"/devices/{encoded_id}/tasks",
            params=params,
            json_data=task,
        )
        return response.json() if response.text else {}

    def get_parameter_values(
        self,
        device_id: str,
        parameters: list[str],
        connection_request: bool = True,
    ) -> dict:
        """Get parameter values from device.

        Args:
            device_id: Device ID
            parameters: List of parameter paths
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "getParameterValues", "parameterNames": parameters}
        return self.create_task(device_id, task, connection_request)

    def set_parameter_values(
        self,
        device_id: str,
        parameters: dict[str, Any],
        connection_request: bool = True,
    ) -> dict:
        """Set parameter values on device.

        Args:
            device_id: Device ID
            parameters: Dict of parameter path -> value
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        # GenieACS expects [[path, value, type], ...]
        param_list = [[k, v, "xsd:string"] for k, v in parameters.items()]
        task = {"name": "setParameterValues", "parameterValues": param_list}
        return self.create_task(device_id, task, connection_request)

    def refresh_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
    ) -> dict:
        """Refresh object tree from device.

        Args:
            device_id: Device ID
            object_path: Object path to refresh (e.g., "Device.WiFi.")
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "refreshObject", "objectName": object_path}
        return self.create_task(device_id, task, connection_request)

    def reboot_device(self, device_id: str, connection_request: bool = True) -> dict:
        """Reboot device.

        Args:
            device_id: Device ID
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "reboot"}
        return self.create_task(device_id, task, connection_request)

    def factory_reset(self, device_id: str, connection_request: bool = True) -> dict:
        """Factory reset device.

        Args:
            device_id: Device ID
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "factoryReset"}
        return self.create_task(device_id, task, connection_request)

    def download(
        self,
        device_id: str,
        file_type: str,
        file_url: str,
        filename: str | None = None,
        connection_request: bool = True,
    ) -> dict:
        """Trigger firmware/config download on device.

        Args:
            device_id: Device ID
            file_type: File type (e.g., "1 Firmware Upgrade Image")
            file_url: URL to download from
            filename: Optional filename
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {
            "name": "download",
            "fileType": file_type,
            "url": file_url,
        }
        if filename:
            task["filename"] = filename
        return self.create_task(device_id, task, connection_request)

    def add_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
    ) -> dict:
        """Add object instance.

        Args:
            device_id: Device ID
            object_path: Object path (e.g., "Device.NAT.PortMapping.")
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "addObject", "objectName": object_path}
        return self.create_task(device_id, task, connection_request)

    def delete_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
    ) -> dict:
        """Delete object instance.

        Args:
            device_id: Device ID
            object_path: Object path (e.g., "Device.NAT.PortMapping.1.")
            connection_request: Whether to trigger connection request

        Returns:
            Task result
        """
        task = {"name": "deleteObject", "objectName": object_path}
        return self.create_task(device_id, task, connection_request)

    def get_pending_tasks(self, device_id: str) -> list[dict]:
        """Get pending tasks for device.

        Args:
            device_id: Device ID

        Returns:
            List of pending tasks
        """
        query = {"device": device_id}
        params = {"query": json.dumps(query)}
        response = self._request("GET", "/tasks", params=params)
        return response.json()

    def delete_task(self, task_id: str) -> None:
        """Delete/cancel a task.

        Args:
            task_id: Task ID
        """
        self._request("DELETE", f"/tasks/{task_id}")

    # -------------------------------------------------------------------------
    # Preset Operations
    # -------------------------------------------------------------------------

    def list_presets(self) -> list[dict]:
        """List all presets.

        Returns:
            List of presets
        """
        response = self._request("GET", "/presets")
        return response.json()

    def get_preset(self, preset_id: str) -> dict:
        """Get preset by ID.

        Args:
            preset_id: Preset ID

        Returns:
            Preset document
        """
        response = self._request("GET", f"/presets/{preset_id}")
        return response.json()

    def create_preset(self, preset: dict) -> dict:
        """Create a preset.

        Args:
            preset: Preset definition

        Returns:
            Created preset
        """
        response = self._request("PUT", f"/presets/{preset['_id']}", json_data=preset)
        return response.json() if response.text else preset

    def delete_preset(self, preset_id: str) -> None:
        """Delete a preset.

        Args:
            preset_id: Preset ID
        """
        self._request("DELETE", f"/presets/{preset_id}")

    # -------------------------------------------------------------------------
    # Provision Operations
    # -------------------------------------------------------------------------

    def list_provisions(self) -> list[dict]:
        """List all provisions.

        Returns:
            List of provisions
        """
        response = self._request("GET", "/provisions")
        return response.json()

    def get_provision(self, provision_id: str) -> dict:
        """Get provision by ID.

        Args:
            provision_id: Provision ID

        Returns:
            Provision document
        """
        response = self._request("GET", f"/provisions/{provision_id}")
        return response.json()

    def create_provision(self, provision_id: str, script: str) -> None:
        """Create/update a provision script.

        Args:
            provision_id: Provision ID
            script: JavaScript provision script
        """
        self._request("PUT", f"/provisions/{provision_id}", content=script)

    def delete_provision(self, provision_id: str) -> None:
        """Delete a provision.

        Args:
            provision_id: Provision ID
        """
        self._request("DELETE", f"/provisions/{provision_id}")

    # -------------------------------------------------------------------------
    # Fault Operations
    # -------------------------------------------------------------------------

    def list_faults(self, device_id: str | None = None) -> list[dict]:
        """List faults, optionally filtered by device.

        Args:
            device_id: Optional device ID filter

        Returns:
            List of faults
        """
        params = {}
        if device_id:
            params["query"] = json.dumps({"device": device_id})

        response = self._request("GET", "/faults", params=params)
        return response.json()

    def delete_fault(self, fault_id: str) -> None:
        """Delete/acknowledge a fault.

        Args:
            fault_id: Fault ID
        """
        encoded_id = quote(fault_id, safe="")
        self._request("DELETE", f"/faults/{encoded_id}")

    def retry_fault(self, fault_id: str) -> None:
        """Retry a faulted task.

        Args:
            fault_id: Fault ID
        """
        encoded_id = quote(fault_id, safe="")
        self._request("POST", f"/faults/{encoded_id}/retry")

    # -------------------------------------------------------------------------
    # Tag Operations
    # -------------------------------------------------------------------------

    def add_tag(self, device_id: str, tag: str) -> None:
        """Add tag to device.

        Args:
            device_id: Device ID
            tag: Tag name
        """
        encoded_id = quote(device_id, safe="")
        self._request("POST", f"/devices/{encoded_id}/tags/{tag}")

    def remove_tag(self, device_id: str, tag: str) -> None:
        """Remove tag from device.

        Args:
            device_id: Device ID
            tag: Tag name
        """
        encoded_id = quote(device_id, safe="")
        self._request("DELETE", f"/devices/{encoded_id}/tags/{tag}")

    # -------------------------------------------------------------------------
    # Helper Methods
    # -------------------------------------------------------------------------

    def build_device_id(self, oui: str, product_class: str, serial_number: str) -> str:
        """Build GenieACS device ID from components.

        Args:
            oui: Device OUI (manufacturer code)
            product_class: Product class
            serial_number: Serial number

        Returns:
            Device ID in format OUI-ProductClass-SerialNumber
        """
        return f"{oui}-{product_class}-{serial_number}"

    def parse_device_id(self, device_id: str) -> tuple[str, str, str]:
        """Parse GenieACS device ID into components.

        Args:
            device_id: Device ID

        Returns:
            Tuple of (oui, product_class, serial_number)

        Raises:
            ValueError: If device ID format is invalid
        """
        parts = device_id.split("-", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid device ID format: {device_id}")
        return parts[0], parts[1], parts[2]

    def extract_parameter_value(self, device: dict, parameter_path: str) -> Any:
        """Extract parameter value from device document.

        GenieACS stores parameters in a nested structure. This helper
        navigates the structure to extract the actual value.

        Args:
            device: Device document
            parameter_path: Parameter path (e.g., "Device.DeviceInfo.SerialNumber")

        Returns:
            Parameter value or None if not found
        """
        parts = parameter_path.split(".")
        current = device

        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None

        # GenieACS stores values in _value field
        if isinstance(current, dict) and "_value" in current:
            return current["_value"]

        return current
