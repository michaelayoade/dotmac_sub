"""Shared helpers for ONT and CPE actions executed through GenieACS."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import CPEDevice, OntUnit
from app.services.network._resolve import (
    resolve_genieacs_for_cpe_with_reason,
    resolve_genieacs_with_reason,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Result of a remote ONT action."""

    success: bool
    message: str
    data: dict[str, Any] | None = None
    waiting: bool = False


@dataclass
class DeviceConfig:
    """Structured running config from an ONT."""

    device_info: dict[str, Any]
    wan: dict[str, Any]
    optical: dict[str, Any]
    wifi: dict[str, Any]
    raw: dict[str, Any]


TR069_ROOT_DEVICE = "Device"
TR069_ROOT_IGD = "InternetGatewayDevice"


def detect_data_model_root(
    db: Session,
    ont: OntUnit | CPEDevice,
    client: Any,
    device_id: str,
) -> str:
    """Detect whether device uses Device (TR-181) or InternetGatewayDevice (TR-098).

    Checks the cached value on the model first, then queries GenieACS.
    Caches the result on the loaded model instance for reuse in the current
    unit of work. Persistence is left to explicit write paths.
    """
    if ont.tr069_data_model in (TR069_ROOT_DEVICE, TR069_ROOT_IGD):
        return ont.tr069_data_model

    try:
        from app.services.genieacs import GenieACSError

        device = client.get_device(device_id)
        if isinstance(device.get("Device"), dict):
            root = TR069_ROOT_DEVICE
        else:
            root = TR069_ROOT_IGD
        ont.tr069_data_model = root
        return root
    except GenieACSError as exc:
        logger.warning(
            "Could not detect data model for ONT %s, defaulting to IGD: %s",
            ont.serial_number,
            exc,
        )
        return TR069_ROOT_IGD


def persist_data_model_root(
    device: OntUnit | CPEDevice,
    root: str,
) -> None:
    """Persist a detected data-model root in an isolated transaction."""
    if root not in (TR069_ROOT_DEVICE, TR069_ROOT_IGD):
        return
    device_id = getattr(device, "id", None)
    if not device_id:
        return

    model_cls = type(device)

    try:
        from app.services.db_session_adapter import db_session_adapter

        db = db_session_adapter.create_session()
        try:
            record = db.get(model_cls, str(device_id))
            if record is None:
                return
            current = getattr(record, "tr069_data_model", None)
            if current == root:
                return
            record.tr069_data_model = root  # type: ignore[attr-defined]
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning(
            "Failed to persist TR-069 data model root %s for %s:%s",
            root,
            model_cls.__name__,
            device_id,
            exc_info=True,
        )


def build_tr069_params(
    root: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build parameter dict using only the detected data model root.

    Args:
        root: "Device" or "InternetGatewayDevice"
        params: Dict of {suffix_path: value} where suffix_path omits the root.
                Example: {"ManagementServer.ConnectionRequestUsername": "acs"}

    Returns:
        Dict of {full_path: value} with the correct root prefix.
    """
    return {f"{root}.{path}": value for path, value in params.items()}


# ---------------------------------------------------------------------------
# Verified TR-069 writes
# ---------------------------------------------------------------------------


def read_param_from_cache(
    client: Any, device_id: str, full_path: str
) -> tuple[Any, str | None]:
    """Read a single parameter's (value, timestamp) from the GenieACS cache.

    Returns (None, None) if the path is not present or the cache cannot be read.
    """
    try:
        device = client.get_device(device_id)
    except Exception as exc:  # noqa: BLE001 - best-effort cache read
        logger.debug(
            "GenieACS device cache read failed for %s path=%s: %s",
            device_id,
            full_path,
            exc,
        )
        return None, None
    node: Any = device
    for part in full_path.split("."):
        if not isinstance(node, dict):
            return None, None
        node = node.get(part)
        if node is None:
            return None, None
    if not isinstance(node, dict) or "_value" not in node:
        return None, None
    return node.get("_value"), node.get("_timestamp")


def values_equal(cache_value: Any, requested: str) -> bool:
    """Compare a cached TR-069 value to the requested string, tolerating bools/ints.

    GenieACS stores booleans as Python bool and integers as int in the cache,
    but the client always writes string values. Normalize both sides so e.g.
    ``"true"`` matches ``True`` and ``"6"`` matches ``6``.
    """
    if cache_value == requested:
        return True
    if isinstance(cache_value, bool):
        return str(cache_value).lower() == str(requested).lower()
    if isinstance(cache_value, (int, float)):
        return str(cache_value) == str(requested)
    if cache_value is None:
        return False
    got = str(cache_value).strip().lower()
    want = str(requested).strip().lower()
    truthy = {"1", "true", "enabled"}
    falsy = {"0", "false", "disabled"}
    if want in truthy:
        return got in truthy
    if want in falsy:
        return got in falsy
    return got == want


def _connection_request_error(task_result: dict[str, object] | None) -> str | None:
    if not isinstance(task_result, dict):
        return None
    error = task_result.get("connectionRequestError")
    if error:
        return str(error)
    return None


def _delete_task_quietly(client: Any, task_result: dict[str, object] | None) -> None:
    if not isinstance(task_result, dict):
        return
    task_id = str(task_result.get("_id") or "").strip()
    if not task_id:
        return
    delete_task = getattr(client, "delete_task", None)
    if not callable(delete_task):
        return
    try:
        delete_task(task_id)
    except Exception:
        logger.debug("Failed to delete ACS task %s", task_id, exc_info=True)


def _wait_for_task(
    client: Any,
    device_id: str,
    task_id: str,
    *,
    timeout_sec: int = 30,
    poll_interval_sec: float = 2.0,
) -> tuple[bool, str]:
    """Poll until a task completes or times out.

    Args:
        client: ACS client.
        device_id: Device ID.
        task_id: Task ID to monitor.
        timeout_sec: Maximum seconds to wait.
        poll_interval_sec: Seconds between polls.

    Returns:
        Tuple of (completed, message).
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            pending = client.get_pending_tasks(device_id)
            task_still_pending = any(t.get("_id") == task_id for t in pending)
            if not task_still_pending:
                return True, "Task completed"
        except Exception as exc:
            logger.debug("Error polling task %s: %s", task_id, exc)
        time.sleep(poll_interval_sec)
    return False, f"Task {task_id} did not complete within {timeout_sec}s"


def set_and_verify(
    client: Any,
    device_id: str,
    params: dict[str, str],
    *,
    expected: dict[str, str] | None = None,
    timeout_sec: int = 30,
    skip_verification: bool = False,
) -> dict[str, object]:
    """Apply params via setParameterValues and poll until completion.

    Uses a polling approach:
      1. Create setParameterValues task.
      2. Poll pending tasks until SPV completes or times out.
      3. Optionally verify values from cache match expected.

    Args:
        client: ACS client.
        device_id: Device ID.
        params: Dict of parameter path -> value.
        expected: Expected values for verification (defaults to params).
        timeout_sec: Max seconds to wait for task completion.
        skip_verification: If True, skip cache verification after completion.

    Returns:
        The SPV task result dict.

    Raises:
        GenieACSError: If task times out or verification fails.
    """
    from app.services.genieacs import GenieACSError  # local import avoids cycle

    if not params:
        raise GenieACSError("set_and_verify called with no parameters")

    expected_values = expected if expected is not None else params

    # Create SPV task
    spv_result: dict[str, object] = client.set_parameter_values(device_id, params)
    task_id = spv_result.get("_id", "")

    if not task_id:
        # Task accepted immediately (no pending task created)
        logger.debug("SPV accepted without task ID for %s", device_id)
        return spv_result

    # Poll until task completes
    completed, msg = _wait_for_task(
        client, device_id, task_id, timeout_sec=timeout_sec
    )
    if not completed:
        _delete_task_quietly(client, spv_result)
        raise GenieACSError(f"setParameterValues task timed out: {msg}")

    if skip_verification or not expected_values:
        return spv_result

    # Verify values from cache
    mismatches: list[str] = []
    for path, want in expected_values.items():
        got, _ = read_param_from_cache(client, device_id, path)
        if values_equal(got, want):
            continue
        mismatches.append(f"{path}: expected={want!r} got={got!r}")

    if mismatches:
        raise GenieACSError(
            "Device did not apply setParameterValues: " + "; ".join(mismatches)
        )
    return spv_result


def get_ont_or_error(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, ActionResult | None]:
    """Load an ONT record or return a standard not-found result."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, ActionResult(success=False, message="ONT not found.")
    return ont, None


def resolve_client_or_error(
    db: Session,
    ont: OntUnit,
) -> tuple[tuple[Any, str] | None, ActionResult | None]:
    """Resolve the GenieACS client/device pair for an ONT."""
    resolved, reason = resolve_genieacs_with_reason(db, ont)
    if not resolved:
        return None, ActionResult(
            success=False,
            message=reason or "No GenieACS server configured for this ONT.",
        )
    return resolved, None


def get_cpe_or_error(
    db: Session, cpe_id: str
) -> tuple[CPEDevice | None, ActionResult | None]:
    """Load a CPE device record or return a standard not-found result."""
    cpe = db.get(CPEDevice, cpe_id)
    if not cpe:
        return None, ActionResult(success=False, message="CPE device not found.")
    return cpe, None


def resolve_cpe_client_or_error(
    db: Session,
    cpe: CPEDevice,
) -> tuple[tuple[Any, str] | None, ActionResult | None]:
    """Resolve the GenieACS client/device pair for a CPE device."""
    resolved, reason = resolve_genieacs_for_cpe_with_reason(db, cpe)
    if not resolved:
        return None, ActionResult(
            success=False,
            message=reason or "No GenieACS server configured for this CPE device.",
        )
    return resolved, None


def get_ont_strict_or_error(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, ActionResult | None]:
    """Load an ONT and narrow away the optional type for callers."""
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return None, error
    if ont is None:
        return None, ActionResult(success=False, message="ONT not found.")
    return ont, None


def get_ont_client_or_error(
    db: Session, ont_id: str
) -> tuple[tuple[OntUnit, Any, str] | None, ActionResult | None]:
    """Load an ONT and resolve its GenieACS client/device id."""
    ont, error = get_ont_strict_or_error(db, ont_id)
    if error:
        return None, error
    if ont is None:
        return None, ActionResult(success=False, message="ONT not found.")
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        if error.message.startswith("No TR-069 device found in GenieACS"):
            return None, ActionResult(
                success=False,
                message=(
                    f"ONT {ont.serial_number} has no GenieACS identity. Sync-only "
                    "provisioning requires a resolvable ACS device before push."
                ),
                data={"missing_acs_identity": True, "serial": ont.serial_number},
                waiting=False,
            )
        return None, error
    if resolved is None:
        return None, ActionResult(
            success=False,
            message="No GenieACS server configured for this ONT.",
        )
    client, device_id = resolved
    return (ont, client, device_id), None


def get_cpe_strict_or_error(
    db: Session, cpe_id: str
) -> tuple[CPEDevice | None, ActionResult | None]:
    """Load a CPE and narrow away the optional type for callers."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return None, error
    if cpe is None:
        return None, ActionResult(success=False, message="CPE device not found.")
    return cpe, None


def get_cpe_client_or_error(
    db: Session, cpe_id: str
) -> tuple[tuple[CPEDevice, Any, str] | None, ActionResult | None]:
    """Load a CPE and resolve its GenieACS client/device id."""
    cpe, error = get_cpe_strict_or_error(db, cpe_id)
    if error:
        return None, error
    if cpe is None:
        return None, ActionResult(success=False, message="CPE device not found.")
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return None, error
    if resolved is None:
        return None, ActionResult(
            success=False,
            message="No GenieACS server configured for this CPE device.",
        )
    client, device_id = resolved
    return (cpe, client, device_id), None
