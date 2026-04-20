"""GenieACS API client for TR-069 device management.

This module provides a client for interacting with the GenieACS NBI
(Northbound Interface) to manage TR-069/CWMP devices.
"""

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


def _infer_cwmp_value_type(path: str, value: Any) -> str:
    """Infer a CWMP xsd type for a GenieACS setParameterValues item.

    This function delegates to the TR-069 parameter adapter for type inference.
    The adapter provides comprehensive type detection based on both path patterns
    and parameter registry metadata.

    Args:
        path: Full CWMP parameter path
        value: Value to analyze

    Returns:
        xsd type string (e.g., "xsd:boolean", "xsd:string", "xsd:unsignedInt")
    """
    from app.services.network.tr069_parameter_adapter import infer_cwmp_type_string

    return infer_cwmp_type_string(path, value)


def normalize_tr069_serial(value: str | None) -> str:
    """Normalize device serials for cross-system matching."""
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


class GenieACSError(Exception):
    """Base exception for GenieACS client errors."""

    pass


class GenieACSMethodNotAllowedError(GenieACSError):
    """Raised when a GenieACS endpoint rejects the HTTP method."""

    pass


class GenieACSTaskRejectedError(GenieACSError):
    """Raised when a task is rejected by local queue-safety policy."""

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
            # 405 is expected for some endpoints (handled with fallback), log at debug
            if e.response.status_code == 405:
                logger.debug("GenieACS 405 on %s %s (will use fallback)", method, path)
                raise GenieACSMethodNotAllowedError("API error: 405") from e
            logger.error(
                f"GenieACS API error: {e.response.status_code} - {e.response.text}"
            )
            raise GenieACSError(f"API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error(f"GenieACS request error: {e}")
            raise GenieACSError(f"Request error: {e}") from e

    # -------------------------------------------------------------------------
    # Device Operations
    # -------------------------------------------------------------------------

    def list_devices(
        self, query: dict | None = None, projection: dict | None = None
    ) -> list[dict[str, Any]]:
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
        return cast(list[dict[str, Any]], response.json())

    def get_device(self, device_id: str) -> dict[str, Any]:
        """Get device by ID through the portable GenieACS collection query API.

        Args:
            device_id: Device ID (format: OUI-ProductClass-SerialNumber)

        Returns:
            Device document

        Raises:
            GenieACSError: If device not found or request fails
        """
        devices = self.list_devices(query={"_id": device_id})
        if devices:
            return devices[0]

        parts = device_id.rsplit("-", 1)
        if len(parts) == 2:
            serial_suffix = parts[1]
            devices = self.list_devices(
                query={"_id": {"$regex": f".*-{re.escape(serial_suffix)}$"}}
            )
            if devices:
                return devices[0]

        raise GenieACSError(f"Device not found: {device_id}")

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

    @staticmethod
    def _task_signature(task: dict[str, Any]) -> tuple[object, ...]:
        return (
            task.get("name"),
            tuple(task.get("parameterNames") or []),
            tuple(tuple(item) for item in task.get("parameterValues") or []),
            task.get("objectName"),
            task.get("fileType"),
            task.get("fileName"),
        )

    @staticmethod
    def _task_parameter_names(task: dict[str, Any]) -> tuple[str, ...]:
        if task.get("name") == "setParameterValues":
            return tuple(
                str(item[0])
                for item in task.get("parameterValues") or []
                if isinstance(item, list | tuple) and item
            )
        return tuple(str(item) for item in task.get("parameterNames") or [])

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(str(os.getenv(name, "")).strip() or default)
        except ValueError:
            return default

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = str(os.getenv(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    @classmethod
    def _max_pending_tasks_per_device(cls) -> int:
        return max(cls._env_int("GENIEACS_MAX_PENDING_TASKS_PER_DEVICE", 20), 1)

    @classmethod
    def _pending_task_ttl(cls) -> timedelta:
        seconds = cls._env_int("GENIEACS_PENDING_TASK_TTL_SECONDS", 1800)
        return timedelta(seconds=max(seconds, 60))

    @staticmethod
    def _is_broad_refresh_task(task: dict[str, Any]) -> bool:
        if task.get("name") != "refreshObject":
            return False
        object_name = str(task.get("objectName") or "").strip().rstrip(".")
        return object_name in {"Device", "InternetGatewayDevice"}

    @staticmethod
    def _is_inform_safe_deferred_task(task: dict[str, Any]) -> bool:
        """Return true for task types that should wait until ACS backlog is clear."""
        return str(task.get("name") or "").strip() in {
            "getParameterValues",
            "refreshObject",
        }

    @classmethod
    def _inform_safe_mode_enabled(cls) -> bool:
        return cls._env_bool("GENIEACS_INFORM_SAFE_MODE", True)

    def _delete_stale_pending_tasks(
        self,
        device_id: str,
        pending_tasks: list[dict[str, Any]],
        *,
        older_than: timedelta,
    ) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - older_than
        active_tasks: list[dict[str, Any]] = []
        for pending_task in pending_tasks:
            timestamp = self._parse_timestamp(pending_task.get("timestamp"))
            task_id = str(pending_task.get("_id") or "").strip()
            if timestamp and timestamp < cutoff and task_id:
                try:
                    self.delete_task(task_id)
                    logger.info(
                        "Deleted stale pending GenieACS task %s for %s",
                        task_id,
                        device_id,
                    )
                    continue
                except GenieACSError:
                    logger.warning(
                        "Failed to delete stale pending GenieACS task %s for %s",
                        task_id,
                        device_id,
                        exc_info=True,
                    )
            active_tasks.append(pending_task)
        return active_tasks

    def _prepare_pending_tasks_for_create(
        self,
        device_id: str,
        task: dict[str, Any],
        *,
        dedupe_pending: bool,
        enforce_safety: bool,
        allow_broad_refresh: bool,
        max_pending_tasks: int | None,
        allow_when_pending: bool,
    ) -> None | dict[str, Any]:
        if enforce_safety and self._is_broad_refresh_task(task) and not allow_broad_refresh:
            raise GenieACSTaskRejectedError(
                "Broad root refreshObject tasks are blocked because they can slow or "
                "fail the next inform. Refresh a targeted object instead."
            )

        if not (dedupe_pending or enforce_safety):
            return None

        pending_tasks = [
            item for item in self.get_pending_tasks(device_id) if isinstance(item, dict)
        ]
        if enforce_safety:
            pending_tasks = self._delete_stale_pending_tasks(
                device_id,
                pending_tasks,
                older_than=self._pending_task_ttl(),
            )

        if dedupe_pending:
            signature = self._task_signature(task)
            retained_tasks: list[dict[str, Any]] = []
            for pending_task in pending_tasks:
                if (
                    task.get("name") == "setParameterValues"
                    and pending_task.get("name") == "setParameterValues"
                    and self._task_parameter_names(pending_task)
                    == self._task_parameter_names(task)
                ):
                    pending_id = str(pending_task.get("_id") or "").strip()
                    if pending_id:
                        logger.info(
                            "Replacing pending GenieACS write task %s for %s",
                            pending_id,
                            device_id,
                        )
                        self.delete_task(pending_id)
                    continue
                if self._task_signature(pending_task) == signature:
                    logger.info(
                        "Reusing pending GenieACS task %s for %s (%s)",
                        pending_task.get("_id"),
                        device_id,
                        task.get("name"),
                    )
                    result = dict(pending_task)
                    result["alreadyPending"] = True
                    return result
                retained_tasks.append(pending_task)
            pending_tasks = retained_tasks

        if enforce_safety:
            if (
                self._inform_safe_mode_enabled()
                and pending_tasks
                and self._is_inform_safe_deferred_task(task)
                and not allow_when_pending
            ):
                raise GenieACSTaskRejectedError(
                    f"Device {device_id} already has {len(pending_tasks)} pending "
                    "GenieACS task(s). Inform-safe mode blocks read/refresh tasks "
                    "until the device backlog is clear."
                )
            limit = max_pending_tasks or self._max_pending_tasks_per_device()
            if len(pending_tasks) >= limit:
                raise GenieACSTaskRejectedError(
                    f"Device {device_id} already has {len(pending_tasks)} pending "
                    f"GenieACS task(s), limit is {limit}. Clear stale tasks before "
                    "queueing more work."
                )
        return None

    def create_task(
        self,
        device_id: str,
        task: dict,
        connection_request: bool = True,
        dedupe_pending: bool = True,
        enforce_safety: bool = True,
        allow_broad_refresh: bool = False,
        max_pending_tasks: int | None = None,
        allow_when_pending: bool = False,
    ) -> dict:
        """Create a task for a device.

        Args:
            device_id: Device ID
            task: Task definition
            connection_request: Whether to trigger connection request

        Returns:
            Task result dict, may include 'connectionRequestError' if device
            was unreachable when connection_request=True
        """
        prepared = self._prepare_pending_tasks_for_create(
            device_id,
            task,
            dedupe_pending=dedupe_pending,
            enforce_safety=enforce_safety,
            allow_broad_refresh=allow_broad_refresh,
            max_pending_tasks=max_pending_tasks,
            allow_when_pending=allow_when_pending,
        )
        if prepared is not None:
            return prepared

        encoded_id = quote(device_id, safe="")
        params = {"connection_request": str(connection_request).lower()}

        response = self._request(
            "POST",
            f"/devices/{encoded_id}/tasks",
            params=params,
            json_data=task,
        )
        result = response.json() if response.text else {}

        # GenieACS returns 202 even when device is offline, with error in reason phrase
        # e.g., "HTTP/1.1 202 Device is offline" or "202 Connection request error: ..."
        # Capture this in the result for callers to handle
        if response.status_code == 202 and hasattr(response, "reason_phrase"):
            reason = response.reason_phrase or ""
            # Check for error indicators in the reason phrase
            error_indicators = [
                "offline",
                "error",
                "EHOSTUNREACH",
                "ECONNREFUSED",
                "timeout",
                "unreachable",
            ]
            if any(ind.lower() in reason.lower() for ind in error_indicators):
                # Only set if not already present from JSON body
                if "connectionRequestError" not in result:
                    result["connectionRequestError"] = reason

        return result

    def get_parameter_values(
        self,
        device_id: str,
        parameters: list[str],
        connection_request: bool = True,
        allow_when_pending: bool = False,
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
        return self.create_task(
            device_id,
            task,
            connection_request,
            allow_when_pending=allow_when_pending,
        )

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
        param_list = [
            [k, v, _infer_cwmp_value_type(k, v)] for k, v in parameters.items()
        ]
        task = {"name": "setParameterValues", "parameterValues": param_list}
        return self.create_task(device_id, task, connection_request)

    def refresh_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
        allow_broad_refresh: bool = False,
        allow_when_pending: bool = False,
    ) -> dict:
        """Refresh object tree from device.

        Args:
            device_id: Device ID
            object_path: Object path to refresh (e.g., "Device.WiFi.")
            connection_request: Whether to trigger connection request

        Returns:
            Task result for the refreshObject task.
        """
        task = {"name": "refreshObject", "objectName": object_path.rstrip(".")}
        return self.create_task(
            device_id,
            task,
            connection_request,
            allow_broad_refresh=allow_broad_refresh,
            allow_when_pending=allow_when_pending,
        )

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
        task = {"name": "addObject", "objectName": object_path.rstrip(".")}
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

    def get_pending_tasks(self, device_id: str) -> list[dict[str, Any]]:
        """Get pending tasks for device.

        Args:
            device_id: Device ID

        Returns:
            List of pending tasks
        """
        query = {"device": device_id}
        params = {"query": json.dumps(query)}
        response = self._request("GET", "/tasks", params=params)
        data = response.json()
        return cast(list[dict[str, Any]], data if isinstance(data, list) else [])

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all pending ACS tasks."""
        response = self._request("GET", "/tasks")
        data = response.json()
        return cast(list[dict[str, Any]], data if isinstance(data, list) else [])

    def delete_task(self, task_id: str) -> None:
        """Delete/cancel a task.

        Args:
            task_id: Task ID
        """
        self._request("DELETE", f"/tasks/{task_id}")

    def delete_stale_tasks(
        self,
        *,
        older_than: timedelta,
        dry_run: bool = False,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete pending tasks older than a cutoff.

        Stale pending tasks slow down the next device inform because GenieACS
        drains queued work when the CPE contacts the ACS.
        """
        cutoff = datetime.now(UTC) - older_than
        tasks = self.get_pending_tasks(device_id) if device_id else self.list_tasks()
        stale: list[dict[str, Any]] = []
        for task in tasks:
            timestamp = self._parse_timestamp(task.get("timestamp"))
            if timestamp and timestamp < cutoff:
                stale.append(task)

        deleted = 0
        errors: list[dict[str, str]] = []
        if not dry_run:
            for task in stale:
                task_id = str(task.get("_id") or "").strip()
                if not task_id:
                    continue
                try:
                    self.delete_task(task_id)
                    deleted += 1
                except GenieACSError as exc:
                    errors.append({"task_id": task_id, "error": str(exc)})

        return {
            "matched": len(stale),
            "deleted": deleted,
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "errors": errors,
        }

    # -------------------------------------------------------------------------
    # Preset Operations
    # -------------------------------------------------------------------------

    def list_presets(self) -> list[dict[str, Any]]:
        """List all presets.

        Returns:
            List of presets
        """
        response = self._request("GET", "/presets")
        return cast(list[dict[str, Any]], response.json())

    def get_preset(self, preset_id: str) -> dict[str, Any]:
        """Get preset by ID.

        Args:
            preset_id: Preset ID

        Returns:
            Preset document
        """
        response = self._request("GET", f"/presets/{preset_id}")
        return cast(dict[str, Any], response.json())

    def create_preset(self, preset: dict[str, Any]) -> dict[str, Any]:
        """Create a preset.

        Args:
            preset: Preset definition

        Returns:
            Created preset
        """
        response = self._request("PUT", f"/presets/{preset['_id']}", json_data=preset)
        if not response.text:
            return preset
        return cast(dict[str, Any], response.json())

    def delete_preset(self, preset_id: str) -> None:
        """Delete a preset.

        Args:
            preset_id: Preset ID
        """
        self._request("DELETE", f"/presets/{preset_id}")

    # -------------------------------------------------------------------------
    # Provision Operations
    # -------------------------------------------------------------------------

    def list_provisions(self) -> list[dict[str, Any]]:
        """List all provisions.

        Returns:
            List of provisions
        """
        response = self._request("GET", "/provisions")
        return cast(list[dict[str, Any]], response.json())

    def get_provision(self, provision_id: str) -> dict[str, Any]:
        """Get provision by ID.

        Args:
            provision_id: Provision ID

        Returns:
            Provision document
        """
        response = self._request("GET", f"/provisions/{provision_id}")
        return cast(dict[str, Any], response.json())

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

    def list_faults(self, device_id: str | None = None) -> list[dict[str, Any]]:
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
        return cast(list[dict[str, Any]], response.json())

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

    def clear_device_faults(self, device_id: str) -> int:
        """Clear all faults for a device, returning the number cleared.

        Args:
            device_id: Device ID

        Returns:
            Number of faults cleared
        """
        faults = self.list_faults(device_id)
        cleared = 0
        for fault in faults:
            fault_id = fault.get("_id", "")
            if fault_id:
                try:
                    self.delete_fault(fault_id)
                    cleared += 1
                except GenieACSError:
                    logger.warning("Failed to clear fault %s", fault_id)
        return cleared

    def wait_for_task_completion(
        self,
        device_id: str,
        task_id: str,
        *,
        timeout_sec: int = 30,
    ) -> tuple[bool, str]:
        """Poll until a task completes or times out.

        Args:
            device_id: Device ID
            task_id: Task ID to monitor

        Returns:
            Tuple of (completed, message)
        """
        import time

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            pending = self.get_pending_tasks(device_id)
            task_still_pending = any(t.get("_id") == task_id for t in pending)
            if not task_still_pending:
                return True, "Task completed"
            time.sleep(2)
        return False, f"Task {task_id} did not complete within {timeout_sec}s"

    def set_parameter_values_and_wait(
        self,
        device_id: str,
        parameters: dict[str, Any],
        *,
        connection_request: bool = True,
        timeout_sec: int = 30,
    ) -> tuple[bool, str, dict]:
        """Set parameter values and wait for the task to complete.

        Args:
            device_id: Device ID
            parameters: Dict of parameter path -> value
            connection_request: Whether to trigger connection request
            timeout_sec: Max seconds to wait for completion

        Returns:
            Tuple of (success, message, task_result)
        """
        task_result = self.set_parameter_values(
            device_id, parameters, connection_request
        )
        task_id = task_result.get("_id", "")
        if not task_id:
            return True, "Task accepted (no ID returned)", task_result

        ok, msg = self.wait_for_task_completion(
            device_id, task_id, timeout_sec=timeout_sec
        )
        return ok, msg, task_result

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

    def extract_parameter_value(
        self, device: dict[str, Any], parameter_path: str
    ) -> Any:
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
        current: object = device

        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None

        # GenieACS stores values in _value field
        if isinstance(current, dict) and "_value" in current:
            return current["_value"]

        # Parameter nodes without _value are metadata objects, not usable values.
        if isinstance(current, dict):
            return None

        return current
