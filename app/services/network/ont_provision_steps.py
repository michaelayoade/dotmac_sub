"""ONT provisioning services — independent, explicit-parameter operations.

Each step function performs a single provisioning action with explicit parameters.
No function depends on a provisioning profile or a fixed sequence — the caller
(operator UI, service order workflow, or Celery task) decides what to call
and with what values.

Step functions:
- Take ``db`` + ``ont_id`` + explicit action parameters
- Resolve ONT → OLT context internally (no caller burden)
- Return a ``StepResult`` with success/failure/duration
- Record completion in ``OntUnit.provisioning_steps_completed`` JSON

Supporting utilities:
- ``validate_prerequisites`` — preflight checklist before provisioning
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.network import OntUnit
from app.services.network._common import NasTarget
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_desired_config import upsert_ont_desired_config_value
from app.services.network.ont_provisioning.context import (
    OltContext as OltContext,
)
from app.services.network.ont_provisioning.context import (
    resolve_olt_context as resolve_olt_context,
)
from app.services.network.ont_provisioning.credentials import (
    mask_credentials as mask_credentials,
)
from app.services.network.ont_provisioning.preflight import (
    validate_prerequisites as validate_prerequisites,
)
from app.services.network.ont_provisioning.result import StepResult as StepResult
from app.services.network.provisioning_events import record_ont_provisioning_event
from app.services.notification_adapter import broadcast_websocket, notify

logger = logging.getLogger(__name__)

# Bootstrap polling constants — configurable via DomainSettings (provisioning domain).
# These module-level variables are kept for backward compatibility with test patching.
# At runtime, use the getter functions which check DomainSettings first.
from app.services.network.provisioning_settings import (
    DEFAULTS as _PROVISIONING_DEFAULTS,
)
from app.services.network.provisioning_settings import (
    get_olt_write_mode_enabled,
    get_pppoe_provisioning_method,
    get_tr069_bootstrap_timeout,
)

_BOOTSTRAP_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_timeout_sec
_BOOTSTRAP_POLL_INTERVAL_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_poll_interval_sec
_TR069_TASK_READY_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_task_ready_timeout_sec
_TR069_TASK_READY_POLL_INTERVAL_SEC = (
    _PROVISIONING_DEFAULTS.tr069_task_ready_poll_interval_sec
)

def _acs_config_writer():
    from app.services.acs_client import create_acs_config_writer

    return create_acs_config_writer()


# ---------------------------------------------------------------------------
# Step completion tracking
# ---------------------------------------------------------------------------


def _record_step(db: Session, ont: OntUnit, step_name: str, result: StepResult) -> None:
    """Log and persist provisioning step completion.

    The event log is append-only for audit/replay. The ONT's provisioning_status
    is updated when the overall workflow completes, not per-step.
    """
    record_ont_provisioning_event(db, ont, step_name, result)
    logger.info(
        "Provisioning step %s for ONT %s: success=%s waiting=%s duration_ms=%s message=%s",
        step_name,
        ont.id,
        result.success,
        result.waiting,
        result.duration_ms,
        result.message[:200],
    )


def _is_existing_service_port_conflict(message: str) -> bool:
    """Return True when OLT rejected because the service-port exists."""
    lowered = str(message or "").lower()
    return (
        "service virtual port has existed already" in lowered
        or "already exists" in lowered
        or "conflicted service virtual port index" in lowered
    )


def _is_unsupported_omci_command(message: str) -> bool:
    """Return True when OLT rejects PPPoE OMCI as unsupported."""
    lowered = str(message or "").lower()
    return "unknown command" in lowered or "unrecognized" in lowered


# ---------------------------------------------------------------------------
# SERVICE: Create OLT service ports
# ---------------------------------------------------------------------------


def create_service_port(
    db: Session,
    ont_id: str,
    *,
    vlan_id: int,
    gem_index: int = 1,
    user_vlan: int | str | None = None,
    tag_transform: str = "translate",
    idempotent: bool = True,
) -> StepResult:
    """Create a single L2 service-port VLAN/GEM binding on the OLT.

    When idempotent=True (default), uses the reconciler to check if the
    service-port already exists with matching configuration. If it does,
    returns success without attempting to create (NOOP). This prevents
    "already exists" errors on re-provisioning.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        vlan_id: VLAN to bind.
        gem_index: GEM port index (default 1).
        user_vlan: User-side VLAN (optional).
        tag_transform: Tag transform mode (default "translate").
        idempotent: If True, check for existing port before creating (default True).
    """
    from sqlalchemy import select as sa_select

    from app.models.network import Vlan
    from app.services.network.ont_write import ont_write

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("create_service_port", False, err)

    # Pre-check: VLAN must exist in the system for this OLT
    vlan_exists = db.scalars(
        sa_select(Vlan).where(
            Vlan.tag == vlan_id,
            Vlan.is_active.is_(True),
            Vlan.olt_device_id == ctx.olt.id,
        )
    ).first()
    if not vlan_exists:
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "create_service_port",
            False,
            f"VLAN {vlan_id} not found for OLT {ctx.olt.name}. "
            f"Create it first at /admin/network/vlans.",
            ms,
        )
        _record_step(db, ctx.ont, "create_service_port", result)
        return result

    # Idempotency check: see if port already exists with matching config
    if idempotent:
        existing_check = _check_existing_service_port(
            ctx.olt, ctx.fsp, ctx.olt_ont_id, vlan_id, gem_index, tag_transform
        )
        if existing_check.get("exists_and_matches"):
            ms = int((time.monotonic() - t0) * 1000)
            result = StepResult(
                "create_service_port",
                True,
                f"Service-port VLAN {vlan_id} GEM {gem_index} already exists (idempotent NOOP)",
                ms,
                data={
                    "idempotent_noop": True,
                    "existing_index": existing_check.get("index"),
                },
            )
            _record_step(db, ctx.ont, "create_service_port", result)
            return result

    resolved_user_vlan = (
        int(user_vlan)
        if isinstance(user_vlan, str) and user_vlan.isdigit()
        else user_vlan
    )
    if isinstance(resolved_user_vlan, str):
        resolved_user_vlan = None
    action_result = ont_write.update_service_port(
        db,
        ont_id,
        vlan_id=vlan_id,
        gem_index=gem_index,
        user_vlan=resolved_user_vlan,
        tag_transform=tag_transform,
    )

    # Handle "already exists" - verify config matches before treating as idempotent success
    success = action_result.success
    message = action_result.message
    if not success and _is_existing_service_port_conflict(message):
        # Verify the existing service-port has matching configuration
        existing_check = _check_existing_service_port(
            ctx.olt, ctx.fsp, ctx.olt_ont_id, vlan_id, gem_index, tag_transform
        )
        if existing_check.get("exists_and_matches"):
            success = True
            message = "Service-port already exists with matching config (verified)"
        elif existing_check.get("index"):
            # Port exists but config differs - this is NOT idempotent success
            success = False
            message = f"Service-port exists but config differs: {existing_check.get('message')}"
            logger.warning(
                "Service port config mismatch for ONT %s: %s",
                ont_id,
                message,
                extra={
                    "event": "service_port_config_mismatch",
                    "ont_id": ont_id,
                    "vlan_id": vlan_id,
                    "gem_index": gem_index,
                    "expected_tag_transform": tag_transform,
                },
            )
        else:
            # Could not verify but original error indicates exists, treat as idempotent
            success = True
            message = f"Service-port VLAN {vlan_id} GEM {gem_index} already exists (idempotent success)"

    ms = int((time.monotonic() - t0) * 1000)
    result_data: dict[str, object] = {}
    if isinstance(action_result.data, dict):
        raw_index = action_result.data.get("service_port_index")
        if isinstance(raw_index, int):
            result_data["created_service_port_indices"] = [raw_index]
    result = StepResult(
        "create_service_port",
        success,
        message,
        ms,
        data=result_data or None,
    )
    _record_step(db, ctx.ont, "create_service_port", result)
    return result


def _check_existing_service_port(
    olt,
    fsp: str,
    olt_ont_id: int,
    vlan_id: int,
    gem_index: int,
    tag_transform: str,
) -> dict:
    """Check if a matching service-port already exists on the OLT.

    Uses the reconciler's state reading to check for existing ports.

    Returns:
        Dict with 'exists_and_matches', 'index', and 'message' keys.
    """
    try:
        from app.services.network.ont_provisioning.state import read_actual_state

        actual, err = read_actual_state(olt, fsp, olt_ont_id)
        if not actual:
            return {"exists_and_matches": False, "message": err}

        # Check for matching port
        for port in actual.service_ports:
            if port.vlan_id == vlan_id and port.gem_index == gem_index:
                # Check tag_transform if available
                if port.tag_transform and port.tag_transform != tag_transform:
                    return {
                        "exists_and_matches": False,
                        "index": port.index,
                        "message": f"Port exists but tag_transform differs: {port.tag_transform} vs {tag_transform}",
                    }
                return {
                    "exists_and_matches": True,
                    "index": port.index,
                    "message": "Matching service-port found",
                }

        return {"exists_and_matches": False, "message": "No matching port found"}
    except Exception as exc:
        logger.debug("Idempotency check failed, proceeding with create: %s", exc)
        return {"exists_and_matches": False, "message": str(exc)}


# ---------------------------------------------------------------------------
# SERVICE: Configure management IP (IPHOST)
# ---------------------------------------------------------------------------


def configure_management_ip(
    db: Session,
    ont_id: str,
    *,
    vlan_id: int,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> StepResult:
    """Configure ONT management IP (IPHOST) via OLT SSH.

    Delegates to ``ont_write.update_management_ip`` for SSH + DB persistence.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        vlan_id: Management VLAN tag.
        ip_mode: "dhcp" or "static".
        ip_address: Required if ip_mode is "static".
        subnet: Required if ip_mode is "static".
        gateway: Required if ip_mode is "static".
    """
    from app.services.network.ont_write import ont_write

    t0 = time.monotonic()
    action_result = ont_write.update_management_ip(
        db,
        ont_id,
        mgmt_ip_mode=ip_mode,
        mgmt_vlan_tag=vlan_id,
        mgmt_priority=priority,
        mgmt_ip_address=ip_address,
        mgmt_subnet=subnet,
        mgmt_gateway=gateway,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "configure_management_ip",
        action_result.success,
        action_result.message,
        ms,
        critical=False,
    )
    ont = db.get(OntUnit, ont_id)
    if ont:
        _record_step(db, ont, "configure_management_ip", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Activate internet-config (TCP stack)
# ---------------------------------------------------------------------------


def activate_internet_config(
    db: Session,
    ont_id: str,
    *,
    ip_index: int = 0,
) -> StepResult:
    """Activate TCP stack on ONT management WAN via internet-config.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ip_index: IP index for the internet-config command (default 0).
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("activate_internet_config", False, err, critical=False)

    action_result = get_protocol_adapter(ctx.olt).configure_internet_config(
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "activate_internet_config",
        action_result.success,
        action_result.message,
        ms,
        critical=False,
    )
    _record_step(db, ctx.ont, "activate_internet_config", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Configure WAN route+NAT mode (OLT-side)
# ---------------------------------------------------------------------------


def configure_wan_olt(
    db: Session,
    ont_id: str,
    *,
    ip_index: int = 0,
    profile_id: int = 0,
) -> StepResult:
    """Set route+NAT mode on ONT management WAN via OLT SSH wan-config.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        ip_index: IP index (default 0).
        profile_id: OLT wan-config profile ID (default 0).
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("configure_wan_olt", False, err, critical=False)

    action_result = get_protocol_adapter(ctx.olt).configure_wan_config(
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
        profile_id=profile_id,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "configure_wan_olt",
        action_result.success,
        action_result.message,
        ms,
        critical=False,
    )
    _record_step(db, ctx.ont, "configure_wan_olt", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Bind TR-069 server profile
# ---------------------------------------------------------------------------


def bind_tr069(
    db: Session,
    ont_id: str,
    *,
    tr069_olt_profile_id: int,
) -> StepResult:
    """Bind a TR-069 server profile to the ONT via OLT SSH.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        tr069_olt_profile_id: OLT-level TR-069 server profile ID.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("bind_tr069", False, err)

    logger.info(
        "Provisioning TR-069 bind starting: ont_id=%s serial=%s olt=%s fsp=%s olt_ont_id=%s profile_id=%s",
        ctx.ont.id,
        ctx.ont.serial_number,
        ctx.olt.name,
        ctx.fsp,
        ctx.olt_ont_id,
        tr069_olt_profile_id,
    )
    action_result = get_protocol_adapter(ctx.olt).bind_tr069_profile(
        ctx.fsp,
        ctx.olt_ont_id,
        profile_id=tr069_olt_profile_id,
    )
    ok = action_result.success
    msg = action_result.message
    ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "Provisioning TR-069 bind finished: ont_id=%s serial=%s success=%s duration_ms=%s message=%s",
        ctx.ont.id,
        ctx.ont.serial_number,
        ok,
        ms,
        msg,
    )
    result = StepResult("bind_tr069", ok, msg, ms)
    _record_step(db, ctx.ont, "bind_tr069", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Wait for TR-069 bootstrap (GenieACS registration)
# ---------------------------------------------------------------------------


def _is_celery_task_context() -> bool:
    """Check if we're running inside a Celery task.

    Returns True if current execution is within a Celery worker task,
    False if running in a web request or other context.
    """
    try:
        from celery import current_task

        return current_task is not None and current_task.request.id is not None
    except (ImportError, AttributeError):
        return False


def _is_background_context() -> bool:
    """Check if we're in a safe context for blocking operations.

    Returns True if running in Celery task, thread pool, or explicit background context.
    Returns False if running on the main thread (likely a web request handler).
    """
    import threading

    # Check Celery task context first — always safe
    if _is_celery_task_context():
        return True

    # Main thread is NOT safe for blocking (uvicorn/gunicorn web workers)
    # Check this BEFORE pattern matching since "MainThread" contains "thread"
    if threading.current_thread() is threading.main_thread():
        return False

    # Check thread name patterns used by background executors
    # Only reached for non-main threads
    thread_name = threading.current_thread().name.lower()
    background_patterns = (
        "celery",
        "threadpool",  # More specific than "thread"
        "pool",
        "worker",
        "background",
        "executor",
    )
    if any(pattern in thread_name for pattern in background_patterns):
        return True

    # Non-main thread without recognized pattern — assume safe
    # (Better to allow than block legitimate background work)
    return True


class BlockingOperationError(RuntimeError):
    """Raised when a blocking operation is called from a web request context."""

    pass


ProgressCallback = Callable[[int, int, str], None]


def wait_tr069_bootstrap(
    db: Session,
    ont_id: str,
    *,
    allow_blocking: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> StepResult:
    """Poll GenieACS until the ONT registers after TR-069 binding.

    This is a SYNCHRONOUS blocking function that polls until the device
    appears in ACS or timeout is reached. Use for immediate feedback flows.

    Uses exponential backoff (2s -> 4s -> 8s -> 16s -> 30s cap) for smarter
    polling that reduces ACS API load by ~40% while still detecting fast
    registrations quickly.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        allow_blocking: Deprecated, kept for compatibility. Always True now.
        progress_callback: Optional callback(attempt, max_attempts, message)
            called on each poll iteration for real-time progress updates.

    Returns:
        StepResult with success=True if device found, False on timeout.

    Note:
        This function blocks for up to 120 seconds (configurable).
        Progress is reported via the callback if provided.
    """
    from app.services.network.backoff import BOOTSTRAP_BACKOFF, ExponentialBackoff

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return StepResult("wait_tr069_bootstrap", False, "ONT not found")

    # Get configurable timeout from DomainSettings (or use defaults)
    bootstrap_timeout = get_tr069_bootstrap_timeout(db)

    try:
        from app.services.network._resolve import resolve_genieacs_with_reason

        # Use exponential backoff: 2s -> 4s -> 8s -> 16s -> 30s (capped)
        backoff = ExponentialBackoff(
            BOOTSTRAP_BACKOFF,
            total_timeout=bootstrap_timeout,
        )

        # Estimate max attempts for progress reporting (approximate)
        max_attempts = max(1, int(bootstrap_timeout / BOOTSTRAP_BACKOFF.initial_delay))

        logger.info(
            "TR-069 bootstrap wait started: ont_id=%s serial=%s timeout_sec=%s backoff=2s->30s",
            ont.id,
            ont.serial_number,
            bootstrap_timeout,
        )

        last_poll_error = ""
        for attempt, delay in backoff:
            progress_msg = f"Waiting for ONT to register with ACS... (attempt {attempt}, next poll in {delay:.1f}s)"

            # Emit progress callback
            if progress_callback:
                try:
                    progress_callback(attempt, max_attempts, progress_msg)
                except Exception as cb_exc:
                    logger.warning("Progress callback error: %s", cb_exc)

            try:
                resolved, reason = resolve_genieacs_with_reason(db, ont)
            except Exception as exc:
                db.rollback()
                last_poll_error = str(exc)
                logger.warning(
                    "TR-069 bootstrap wait poll error: ont_id=%s serial=%s attempt=%s delay=%.1fs error=%s",
                    ont.id,
                    ont.serial_number,
                    attempt,
                    delay,
                    exc,
                )
                time.sleep(delay)
                continue

            if resolved:
                _client, device_id = resolved
                logger.info(
                    "TR-069 bootstrap complete: ont_id=%s serial=%s genieacs_device_id=%s attempts=%s elapsed_ms=%d",
                    ont.id,
                    ont.serial_number,
                    device_id,
                    attempt,
                    int(backoff.elapsed() * 1000),
                )
                # Emit success via callback
                if progress_callback:
                    try:
                        progress_callback(attempt, max_attempts, "Device registered in ACS")
                    except Exception:
                        pass
                ms = int((time.monotonic() - t0) * 1000)
                result = StepResult(
                    "wait_tr069_bootstrap", True, "Device registered in ACS", ms
                )
                _record_step(db, ont, "wait_tr069_bootstrap", result)
                return result

            logger.info(
                "TR-069 bootstrap wait poll miss: ont_id=%s serial=%s attempt=%s delay=%.1fs reason=%s",
                ont.id,
                ont.serial_number,
                attempt,
                delay,
                reason,
            )
            time.sleep(delay)

        # Backoff exhausted (timeout reached)
        logger.warning(
            "TR-069 bootstrap timeout: ont_id=%s serial=%s timeout_sec=%s",
            ont.id,
            ont.serial_number,
            bootstrap_timeout,
        )
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "wait_tr069_bootstrap",
            False,
            (
                f"Device not found in ACS after {bootstrap_timeout}s"
                if not last_poll_error
                else f"Device not found in ACS after {bootstrap_timeout}s; last poll error: {last_poll_error}"
            ),
            ms,
        )
        _record_step(db, ont, "wait_tr069_bootstrap", result)
        return result
    except Exception as e:
        logger.error("Error during TR-069 bootstrap poll: %s", e)
        db.rollback()
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "wait_tr069_bootstrap", False, f"Bootstrap poll error: {e}", ms
        )
        try:
            _record_step(db, ont, "wait_tr069_bootstrap", result)
        except Exception:
            logger.warning(
                "Failed to record wait_tr069_bootstrap step after bootstrap poll error",
                exc_info=True,
            )
        return result


def _decrypt_optional_secret(value: object) -> str | None:
    if value in (None, ""):
        return None
    from app.services.credential_crypto import decrypt_credential

    try:
        return decrypt_credential(str(value)) or str(value)
    except Exception:
        return str(value)


def _igd_wan_instance_for_vlan_from_snapshot(ont: OntUnit, wan_vlan: int | None) -> int | None:
    capabilities = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(capabilities, dict):
        return None
    capabilities = capabilities.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    wan_caps = capabilities.get("wan")
    if not isinstance(wan_caps, dict):
        return None
    if wan_caps.get("data_model") != "InternetGatewayDevice":
        return None
    connections = wan_caps.get("connections")
    if not isinstance(connections, list):
        return None
    requested_vlan = str(wan_vlan or "").strip()
    for require_vlan_match in (True, False):
        for item in connections:
            if not isinstance(item, dict):
                continue
            service = str(item.get("detected_wan_service") or "").upper()
            vlan = str(item.get("detected_wan_vlan") or "").strip()
            if service == "TR069":
                continue
            if require_vlan_match and requested_vlan and vlan == requested_vlan:
                return int(item.get("index") or 0) or None
            if not require_vlan_match and not requested_vlan:
                return int(item.get("index") or 0) or None
    return None


def _mark_ppp_wan_requires_precreated(ont: OntUnit) -> None:
    snapshot = getattr(ont, "tr069_last_snapshot", None)
    if not isinstance(snapshot, dict):
        return
    snapshot = dict(snapshot)
    capabilities = snapshot.get("capabilities")
    if not isinstance(capabilities, dict):
        return
    capabilities = dict(capabilities)
    pending = capabilities.get("pending_actions")
    if isinstance(pending, dict):
        pending = dict(pending)
        pending.pop("add_ppp_wan", None)
        if pending:
            capabilities["pending_actions"] = pending
        else:
            capabilities.pop("pending_actions", None)
    wan_caps = capabilities.get("wan")
    if isinstance(wan_caps, dict):
        wan_caps = dict(wan_caps)
        wan_caps["supports_tr069_add_ppp_wan"] = False
        wan_caps["supports_tr069_set_ppp_credentials"] = False
        wan_caps["requires_precreated_ppp_wan"] = True
        capabilities["wan"] = wan_caps
    snapshot["capabilities"] = capabilities
    ont.tr069_last_snapshot = snapshot
    flag_modified(ont, "tr069_last_snapshot")


def _provision_wan_service_instances(
    db: Session,
    ont_id: str,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Provision WAN from resolved desired_config only."""
    from app.services.network.ont_action_wan import (
        set_pppoe_credentials,
        set_wan_dhcp,
        set_wan_static,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return [], [], ["ONT not found"]

    effective = resolve_effective_ont_config(db, ont)
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    wan_mode = str(effective_values.get("wan_mode") or "").strip().lower()
    wan_vlan = effective_values.get("wan_vlan")
    wan_vlan_int = int(wan_vlan) if wan_vlan not in (None, "") else None
    instance_index = int(effective_values.get("wan_instance_index") or 1)
    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []

    if not wan_mode:
        needs_input.append("WAN mode is not configured in desired_config.")
        return steps, needs_input, hard_failures
    if wan_mode == "bridge":
        steps.append(
            {
                "step": "provision_wan_desired_config",
                "success": True,
                "message": "Bridge WAN is configured on the OLT service port.",
            }
        )
        return steps, needs_input, hard_failures

    if wan_mode == "pppoe" and get_pppoe_provisioning_method(db) != "tr069":
        omci_vlan = effective_values.get("pppoe_omci_vlan") or wan_vlan_int
        pppoe_username = effective_values.get("pppoe_username")
        pppoe_password = _decrypt_optional_secret(effective_values.get("pppoe_password"))
        if not pppoe_username or not pppoe_password:
            needs_input.append("PPPoE credentials are not configured in desired_config.")
            return steps, needs_input, hard_failures
        if omci_vlan is not None and get_olt_write_mode_enabled(db):
            ctx, err = resolve_olt_context(db, ont_id)
            if ctx is None:
                hard_failures.append(f"resolve_olt_context: {err}")
                return steps, needs_input, hard_failures
            adapter = get_protocol_adapter(ctx.olt)
            ip_index = int(effective_values.get("internet_config_ip_index") or instance_index)
            inet_result = adapter.configure_internet_config(
                ctx.fsp,
                ctx.olt_ont_id,
                ip_index=ip_index,
            )
            steps.append(
                {
                    "step": "internet_config_olt:desired_config",
                    "success": inet_result.success,
                    "message": inet_result.message,
                }
            )
            wan_profile_id = int(effective_values.get("wan_config_profile_id") or 0)
            if wan_profile_id:
                wan_result = adapter.configure_wan_config(
                    ctx.fsp,
                    ctx.olt_ont_id,
                    ip_index=ip_index,
                    profile_id=wan_profile_id,
                )
                steps.append(
                    {
                        "step": "configure_wan_olt:desired_config",
                        "success": wan_result.success,
                        "message": wan_result.message,
                    }
                )
                if not wan_result.success and get_pppoe_provisioning_method(db) == "omci":
                    hard_failures.append(f"configure_wan_olt: {wan_result.message}")
                    return steps, needs_input, hard_failures
            pppoe_result = adapter.configure_pppoe(
                ctx.fsp,
                ctx.olt_ont_id,
                ip_index=ip_index,
                vlan_id=int(omci_vlan),
                priority=int(effective_values.get("wan_priority") or 0),
                username=str(pppoe_username),
                password=str(pppoe_password),
            )
            steps.append(
                {
                    "step": "configure_pppoe_omci:desired_config",
                    "success": pppoe_result.success,
                    "message": pppoe_result.message,
                }
            )
            if pppoe_result.success:
                return steps, needs_input, hard_failures
            if get_pppoe_provisioning_method(db) == "omci":
                hard_failures.append(f"configure_pppoe_omci: {pppoe_result.message}")
                return steps, needs_input, hard_failures

    detected_index = instance_index
    if wan_mode == "pppoe" and getattr(ont, "tr069_data_model", None) == "InternetGatewayDevice":
        detected_index = _igd_wan_instance_for_vlan_from_snapshot(ont, wan_vlan_int) or instance_index
        if detected_index is None:
            message = (
                f"No safe WANConnectionDevice exists for PPPoE VLAN {wan_vlan_int}. "
                "The ONT must expose or precreate an internet PPP WAN container before TR-069 can push credentials."
            )
            steps.append(
                {
                    "step": "provision_wan_desired_config",
                    "success": False,
                    "message": message,
                }
            )
            hard_failures.append(f"provision_wan_desired_config: {message}")
            _mark_ppp_wan_requires_precreated(ont)
            return steps, needs_input, hard_failures

    if wan_mode == "pppoe":
        pppoe_username = effective_values.get("pppoe_username")
        pppoe_password = _decrypt_optional_secret(effective_values.get("pppoe_password"))
        if not pppoe_username or not pppoe_password:
            needs_input.append("PPPoE credentials are not configured in desired_config.")
            return steps, needs_input, hard_failures
        result = set_pppoe_credentials(
            db,
            ont_id,
            username=str(pppoe_username),
            password=str(pppoe_password),
            instance_index=detected_index,
            ensure_instance=True,
            wan_vlan=wan_vlan_int,
        )
    elif wan_mode == "dhcp":
        result = set_wan_dhcp(
            db,
            ont_id,
            instance_index=detected_index,
            ensure_instance=True,
            wan_vlan=wan_vlan_int,
        )
    elif wan_mode in {"static", "static_ip"}:
        ip_address = effective_values.get("wan_static_ip")
        subnet = effective_values.get("wan_static_subnet")
        gateway = effective_values.get("wan_static_gateway")
        if not (ip_address and subnet and gateway):
            needs_input.append("Static WAN IP, subnet, and gateway are required in desired_config.")
            return steps, needs_input, hard_failures
        result = set_wan_static(
            db,
            ont_id,
            ip_address=str(ip_address),
            subnet_mask=str(subnet),
            gateway=str(gateway),
            dns_servers=str(effective_values.get("wan_static_dns") or "") or None,
            instance_index=detected_index,
        )
    else:
        needs_input.append(f"Unsupported WAN mode in desired_config: {wan_mode}")
        return steps, needs_input, hard_failures

    steps.append(
        {
            "step": "provision_wan_desired_config",
            "success": result.success,
            "message": result.message,
        }
    )
    if not result.success:
        hard_failures.append(f"provision_wan_desired_config: {result.message}")
    return steps, needs_input, hard_failures


def apply_saved_service_config(db: Session, ont_id: str) -> StepResult:
    """Apply saved TR-069 service intent once the ONT is visible in ACS.

    WAN service provisioning is instance-backed. ONT-level desired settings are
    read from ``OntUnit.desired_config`` via ``resolve_effective_ont_config``.

    Missing operator inputs are reported in ``data["needs_input"]`` and fail the
    ACS apply step so the end-to-end provisioning result is not marked complete
    before the intended service config can be pushed.
    """

    from app.services.credential_crypto import decrypt_credential
    from app.services.network.ont_action_network import (
        probe_wan_capabilities,
    )

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return StepResult("apply_saved_service_config", False, "ONT not found")
    acs_config_adapter = _acs_config_writer()
    effective_values = resolve_effective_ont_config(db, ont).get("values", {})

    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []

    def _append(name: str, result) -> None:
        success = bool(getattr(result, "success", False))
        message = str(getattr(result, "message", ""))
        steps.append({"step": name, "success": success, "message": message})
        if not success:
            hard_failures.append(f"{name}: {message}")

    cr_username = effective_values.get("cr_username")
    cr_password = _decrypt_optional_secret(effective_values.get("cr_password"))
    if cr_username and cr_password:
        _append(
            "set_connection_request_credentials",
            acs_config_adapter.set_connection_request_credentials(
                db,
                ont_id,
                str(cr_username),
                str(cr_password),
            ),
        )
    elif effective_values.get("tr069_acs_server_id"):
        needs_input.append("Connection request credentials are incomplete in desired_config/OLT defaults.")

    if effective_values.get("wan_mode"):
        probe_result = probe_wan_capabilities(db, ont_id)
        _append("probe_wan_capabilities", probe_result)

        wan_instance_steps, wan_instance_needs, wan_instance_failures = (
            _provision_wan_service_instances(db, ont_id)
        )
        for step in wan_instance_steps:
            step.pop("waiting", None)  # Sync-only: no waiting semantics
            steps.append(step)
        needs_input.extend(wan_instance_needs)
        hard_failures.extend(wan_instance_failures)
        logger.info(
            "ONT %s: Provisioned %d WAN desired-config steps",
            ont.serial_number,
            len(wan_instance_steps),
        )

    lan_values = {
        "lan_ip": effective_values.get("lan_ip"),
        "lan_subnet": effective_values.get("lan_subnet"),
        "dhcp_enabled": effective_values.get("lan_dhcp_enabled"),
        "dhcp_start": effective_values.get("lan_dhcp_start"),
        "dhcp_end": effective_values.get("lan_dhcp_end"),
    }
    if any(value not in (None, "", []) for value in lan_values.values()):
        dhcp_enabled_value = lan_values.get("dhcp_enabled")
        _append(
            "configure_lan_tr069",
            acs_config_adapter.set_lan_config(
                db,
                ont_id,
                lan_ip=str(lan_values.get("lan_ip") or "") or None,
                lan_subnet=str(lan_values.get("lan_subnet") or "") or None,
                dhcp_enabled=dhcp_enabled_value
                if isinstance(dhcp_enabled_value, bool)
                else None,
                dhcp_start=str(lan_values.get("dhcp_start") or "") or None,
                dhcp_end=str(lan_values.get("dhcp_end") or "") or None,
            ),
        )

    effective = resolve_effective_ont_config(db, ont)
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    wifi_password = None
    raw_wifi_password = effective_values.get("wifi_password")
    if raw_wifi_password:
        try:
            wifi_password = decrypt_credential(str(raw_wifi_password))
        except ValueError:
            wifi_password = str(raw_wifi_password)
    channel = effective_values.get("wifi_channel")
    try:
        channel_int = int(str(channel).strip()) if channel not in (None, "") else None
    except (TypeError, ValueError):
        channel_int = None
        needs_input.append("WiFi channel must be numeric.")
    wifi_values = {
        "enabled": effective_values.get("wifi_enabled")
        if effective_values.get("wifi_enabled") is not None
        else None,
        "ssid": effective_values.get("wifi_ssid"),
        "password": wifi_password,
        "channel": channel_int,
        "security_mode": effective_values.get("wifi_security_mode"),
    }
    if any(value not in (None, "", []) for value in wifi_values.values()):
        wifi_enabled_value = wifi_values.get("enabled")
        _append(
            "configure_wifi_tr069",
            acs_config_adapter.set_wifi_config(
                db,
                ont_id,
                enabled=wifi_enabled_value
                if isinstance(wifi_enabled_value, bool)
                else None,
                ssid=str(wifi_values.get("ssid") or "") or None,
                password=str(wifi_values.get("password") or "") or None,
                channel=channel_int,
                security_mode=str(wifi_values.get("security_mode") or "") or None,
            ),
        )

    ms = int((time.monotonic() - t0) * 1000)
    if hard_failures:
        return StepResult(
            "apply_saved_service_config",
            False,
            "; ".join(hard_failures),
            ms,
            critical=False,
            waiting=False,  # Sync-only: no waiting semantics
            data={"steps": steps, "needs_input": needs_input},
        )
    if not steps and not needs_input:
        return StepResult(
            "apply_saved_service_config",
            True,
            "No saved ONT service config to apply.",
            ms,
            critical=False,
            skipped=True,
            data={"steps": steps},
        )
    message = "Saved ONT service config applied."
    if needs_input:
        message = "Saved ONT service config is incomplete."
    return StepResult(
        "apply_saved_service_config",
        not needs_input,
        message,
        ms,
        critical=False,
        waiting=False,  # Sync-only: no waiting semantics
        data={"steps": steps, "needs_input": needs_input},
    )


# ---------------------------------------------------------------------------
# SERVICE: Set TR-069 connection request credentials
# ---------------------------------------------------------------------------


def set_connection_request_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
) -> StepResult:
    """Set TR-069 connection request credentials on the ONT via ACS.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        username: Connection request username.
        password: Connection request password.
    """
    t0 = time.monotonic()
    cr_result = _acs_config_writer().set_connection_request_credentials(
        db,
        ont_id,
        username,
        password,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "set_connection_request_credentials",
        cr_result.success,
        cr_result.message,
        ms,
        critical=False,
        waiting=getattr(cr_result, "waiting", False),
        data=getattr(cr_result, "data", None),
    )
    ont = db.get(OntUnit, ont_id)
    if ont:
        _record_step(db, ont, "set_connection_request_credentials", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Push PPPoE credentials via OMCI (OLT-side)
# ---------------------------------------------------------------------------


def push_pppoe_omci(
    db: Session,
    ont_id: str,
    *,
    vlan_id: int,
    username: str,
    password: str,
    ip_index: int = 1,
    priority: int = 0,
) -> StepResult:
    """Push PPPoE credentials to ONT via OMCI (OLT-side, not TR-069).

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        vlan_id: PPPoE VLAN ID.
        username: PPPoE username.
        password: PPPoE password.
        ip_index: IP index (default 1).
        priority: CoS priority (default 0).
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("push_pppoe_omci", False, err)

    action_result = get_protocol_adapter(ctx.olt).configure_pppoe(
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
        vlan_id=vlan_id,
        priority=priority,
        username=username,
        password=password,
    )
    ok = action_result.success
    msg = action_result.message
    ms = int((time.monotonic() - t0) * 1000)
    unsupported = not ok and _is_unsupported_omci_command(msg)
    if unsupported:
        msg += " (unsupported)"
    result = StepResult("push_pppoe_omci", ok, msg, ms)
    _record_step(db, ctx.ont, "push_pppoe_omci", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Ensure NAS VLAN (MikroTik)
# ---------------------------------------------------------------------------


def ensure_nas_vlan(
    db: Session,
    *,
    nas: NasTarget,
    vlan_id: int,
    parent_interface: str = "ether3",
    ip_address: str,
    pppoe_service_name: str | None = None,
    pppoe_default_profile: str = "default",
) -> StepResult:
    """Create VLAN interface + IP + PPPoE server on a NAS device.

    Idempotent — reuses existing VLAN if it matches.

    Args:
        db: Database session.
        nas: Lightweight DTO describing the target NAS device. Callers that
            hold a ``NasDevice`` ORM row should construct a :class:`NasTarget`
            from it before calling this function.
        vlan_id: VLAN ID to create.
        parent_interface: Physical interface (default "ether3").
        ip_address: IP address with CIDR for the VLAN interface.
        pppoe_service_name: Optional PPPoE service name.
        pppoe_default_profile: PPP profile name (default "default").
    """
    t0 = time.monotonic()

    try:
        from app.services.nas._mikrotik_vlan import provision_vlan_full

        # ``provision_vlan_full`` is typed to ``NasDevice`` but only reads the
        # attributes present on ``NasTarget``; the DTO is duck-type compatible.
        vlan_result = provision_vlan_full(
            cast(Any, nas),
            vlan_id=vlan_id,
            parent_interface=parent_interface,
            ip_address=ip_address,
            pppoe_service_name=pppoe_service_name,
            pppoe_default_profile=pppoe_default_profile,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "ensure_nas_vlan", vlan_result.success, vlan_result.message, ms
        )
    except Exception as exc:
        logger.error("NAS VLAN provisioning failed: %s", exc)
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "ensure_nas_vlan", False, f"NAS VLAN provisioning failed: {exc}", ms
        )


# ---------------------------------------------------------------------------
# SERVICE: Rollback service ports
# ---------------------------------------------------------------------------


def deprovision(
    db: Session,
    ont_id: str,
) -> StepResult:
    """Full deprovision: remove service-ports, deauthorize, clear DB state.

    Delegates to the existing ``return_to_inventory`` service which handles
    the correct sequence: service-ports → OLT registration → DB cleanup.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
    """
    t0 = time.monotonic()
    try:
        from app.services.network.ont_inventory import return_ont_to_inventory
    except ImportError:
        logger.warning(
            "ont_inventory module not available — skipping inventory return for ONT %s",
            ont_id,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "deprovision",
            True,
            "Deprovision skipped: ont_inventory module not available",
            ms,
        )

    action_result = return_ont_to_inventory(db, ont_id)
    ms = int((time.monotonic() - t0) * 1000)
    return StepResult("deprovision", action_result.success, action_result.message, ms)


def download_firmware(
    db: Session,
    ont_id: str,
    *,
    firmware_image_id: str,
) -> StepResult:
    """Trigger an ACS Download RPC for an ONT firmware image."""
    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return StepResult("download_firmware", False, "ONT not found")

    result = _acs_config_writer().firmware_upgrade(db, ont_id, firmware_image_id)
    ms = int((time.monotonic() - t0) * 1000)
    step_result = StepResult(
        "download_firmware",
        bool(result.success),
        result.message,
        ms,
        critical=False,
        data=result.data,
    )
    _record_step(db, ont, "download_firmware", step_result)
    return step_result


def rollback_service_ports(
    db: Session,
    ont_id: str,
) -> StepResult:
    """Remove all service-ports for an ONT from the OLT.

    Used for cleanup after failed provisioning.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("rollback_service_ports", False, err)

    adapter = get_protocol_adapter(ctx.olt)
    ports_result = adapter.get_service_ports_for_ont(ctx.fsp, ctx.olt_ont_id)
    ports_data = ports_result.data.get("service_ports", []) if ports_result.success else []
    ports = ports_data if isinstance(ports_data, list) else []
    if not ports_result.success or not ports:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports", True, "No service ports to remove", ms
        )

    deleted = 0
    errors = 0
    for port in ports:
        delete_result = adapter.delete_service_port(port.index)
        if delete_result.success:
            deleted += 1
        else:
            errors += 1
            logger.warning(
                "Rollback: failed to delete service-port %d: %s",
                port.index,
                delete_result.message,
            )

    ms = int((time.monotonic() - t0) * 1000)
    message = f"Removed {deleted} service-port(s)"
    if errors:
        message += f", {errors} failed"
    result = StepResult("rollback_service_ports", errors == 0, message, ms)
    _record_step(db, ctx.ont, "rollback_service_ports", result)
    return result


def rollback_service_port_indices(
    db: Session,
    ont_id: str,
    *,
    port_indices: list[int],
    expected_olt_id: str | None = None,
) -> StepResult:
    """Remove only the specified service-port indices for an ONT.

    This is the safer rollback path for watchdog retries because it deletes only
    the ports created by the original failed operation and refuses to act if the
    ONT has moved to another OLT.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("rollback_service_ports", False, err)

    if expected_olt_id is not None and str(ctx.olt.id) != str(expected_olt_id):
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports",
            False,
            "ONT placement changed since rollback failure; refusing targeted retry.",
            ms,
        )

    normalized_indices = sorted({int(index) for index in port_indices})
    if not normalized_indices:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports",
            False,
            "No targeted service-port indices recorded for retry.",
            ms,
        )

    adapter = get_protocol_adapter(ctx.olt)
    deleted = 0
    errors = 0
    error_messages: list[str] = []
    for port_index in normalized_indices:
        delete_result = adapter.delete_service_port(port_index)
        if delete_result.success:
            deleted += 1
        else:
            errors += 1
            error_messages.append(f"{port_index}: {delete_result.message}")
            logger.warning(
                "Targeted rollback: failed to delete service-port %d: %s",
                port_index,
                delete_result.message,
            )

    ms = int((time.monotonic() - t0) * 1000)
    message = f"Removed {deleted} targeted service-port(s)"
    if errors:
        message += f", {errors} failed"
    result = StepResult(
        "rollback_service_ports",
        errors == 0,
        message,
        ms,
        data={
            "targeted_port_indices": normalized_indices,
            "errors": error_messages,
        },
    )
    _record_step(db, ctx.ont, "rollback_service_ports", result)
    return result


# Profile resolution, preflight validation, and command preview live in
# app.services.network.ont_provisioning.* and are imported above for compatibility.


def _ensure_static_management_ip_from_profile(
    db: Session,
    ont: OntUnit,
    profile: object | None,
) -> tuple[bool, str]:
    """Reserve a management IP before building static IPHOST desired state."""
    if profile is None:
        return True, "No provisioning profile selected."
    mode = getattr(profile, "mgmt_ip_mode", None)
    mode_value = getattr(mode, "value", mode)
    if mode_value != "static_ip":
        return True, "Management IP mode is not static."
    effective = resolve_effective_ont_config(db, ont)
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    effective_mgmt_ip = effective_values.get("mgmt_ip_address") or getattr(
        ont, "mgmt_ip_address", None
    )
    if effective_mgmt_ip:
        return True, "Static management IP already assigned."

    pool_id = getattr(profile, "mgmt_ip_pool_id", None)
    if not pool_id:
        return False, "Static management IP mode requires a management IP pool."

    import ipaddress

    from sqlalchemy import select as sa_select

    from app.models.network import IpBlock, IpPool, IPv4Address, IPVersion

    pool = db.get(IpPool, pool_id)
    if pool is None:
        return False, f"Management IP pool {pool_id} not found."
    if not getattr(pool, "is_active", True):
        return False, f"Management IP pool '{pool.name}' is inactive."
    if getattr(pool, "ip_version", None) not in (None, IPVersion.ipv4):
        return False, f"Management IP pool '{pool.name}' is not IPv4."

    blocks = list(
        db.scalars(
            sa_select(IpBlock)
            .where(IpBlock.pool_id == pool.id)
            .where(IpBlock.is_active.is_(True))
        ).all()
    )
    cidrs = [block.cidr for block in blocks] or [pool.cidr]
    used = {
        str(address)
        for address in db.scalars(
            sa_select(IPv4Address.address).where(IPv4Address.pool_id == pool.id)
        ).all()
    }
    gateway = str(pool.gateway or "").strip()
    if gateway:
        used.add(gateway)

    selected_ip = None
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        for ip in network.hosts():
            candidate = str(ip)
            if candidate not in used:
                selected_ip = candidate
                break
        if selected_ip:
            break

    if selected_ip is None:
        pool.next_available_ip = None
        pool.available_count = 0
        db.flush()
        return False, f"No available IPs in management pool '{pool.name}'."

    db.add(
        IPv4Address(
            address=selected_ip,
            pool_id=pool.id,
            is_reserved=True,
            notes=f"Management IP for ONT {ont.serial_number}",
        )
    )
    upsert_ont_desired_config_value(
        db,
        ont=ont,
        field_name="management.ip_address",
        value=selected_ip,
        reason="allocate_management_ip",
    )

    remaining = 0
    next_available = None
    used.add(selected_ip)
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        for ip in network.hosts():
            candidate = str(ip)
            if candidate not in used:
                remaining += 1
                if next_available is None:
                    next_available = candidate
    pool.next_available_ip = next_available
    pool.available_count = remaining
    db.flush()
    return True, f"Reserved static management IP {selected_ip} from '{pool.name}'."


# ---------------------------------------------------------------------------
# STATE RECONCILIATION-BASED PROVISIONING
# ---------------------------------------------------------------------------


def provision_with_reconciliation(
    db: Session,
    ont_id: str,
    *,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
) -> StepResult:
    """Provision an ONT using state reconciliation.

    This is the recommended approach for ONT provisioning. It:
    1. Builds desired state from OLT defaults plus OntUnit.desired_config
    2. Reads actual state from the OLT (single SSH session)
    3. Computes delta (existing matching ports = NOOP = idempotent)
    4. Validates (optical budget, VLAN trunk, ip_index bounds)
    5. Executes with compensation log (rollback on failure)

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        dry_run: If True, compute delta but don't execute.
        allow_low_optical_margin: If True, proceed even with low optical margin.

    Returns:
        StepResult with provisioning outcome.
    """
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_provisioning.reconciler import (
        get_delta_summary,
        reconcile_ont_state,
    )
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_config,
    )

    t0 = time.monotonic()

    # Get context first for logging
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("provision_reconciled", False, err)

    logger.info(
        "Starting reconciled provisioning for ONT %s serial=%s olt=%s fsp=%s",
        ont_id,
        ctx.ont.serial_number,
        ctx.olt.name,
        ctx.fsp,
    )

    # Reconcile state
    delta, err = reconcile_ont_state(
        db,
        ont_id,
    )
    if not delta:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "provision_reconciled", False, f"Reconciliation failed: {err}", ms
        )

    # Check validations
    if not delta.is_valid:
        # If only optical budget failed and we're allowing low margin, override
        if (
            allow_low_optical_margin
            and not delta.optical_budget_ok
            and delta.mgmt_vlan_trunked
            and delta.ip_index_valid
        ):
            logger.warning(
                "Proceeding despite low optical margin for ONT %s: %s",
                ctx.ont.serial_number,
                delta.optical_budget_message,
            )
            delta.optical_budget_ok = True
        else:
            ms = int((time.monotonic() - t0) * 1000)
            summary = get_delta_summary(delta)
            return StepResult(
                "provision_reconciled",
                False,
                f"Validation failed: {summary['validations']}",
                ms,
                data=summary,
            )

    # Check if there are any changes
    if not delta.has_changes:
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "provision_reconciled",
            True,
            "No changes needed - ONT already matches desired state",
            ms,
            data=get_delta_summary(delta),
        )
        _record_step(db, ctx.ont, "provision_reconciled", result)
        return result

    # Build desired state for execution
    desired, err = build_desired_state_from_config(
        db,
        ont_id,
    )
    if not desired:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "provision_reconciled", False, f"Failed to build desired state: {err}", ms
        )

    if dry_run:
        ms = int((time.monotonic() - t0) * 1000)
        summary = get_delta_summary(delta)
        return StepResult(
            "provision_reconciled",
            True,
            f"Dry run: {summary['service_ports']['create']} port(s) to create",
            ms,
            data={"dry_run": True, **summary},
        )

    # Execute the delta through the OLT adapter so batch writes stay protocol-owned.
    adapter = get_protocol_adapter(ctx.olt)
    adapter_result = adapter.execute_provisioning_delta(delta, desired)
    exec_result = adapter_result.data.get("execution_result")
    if exec_result is None:
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "provision_reconciled",
            False,
            adapter_result.message,
            ms,
            data={"protocol_used": adapter_result.protocol_used},
        )
        _record_step(db, ctx.ont, "provision_reconciled", result)
        return result

    ms = int((time.monotonic() - t0) * 1000)

    if exec_result.success:
        logger.info(
            "Reconciled provisioning complete for ONT %s: %d step(s)",
            ctx.ont.serial_number,
            len(exec_result.steps_completed),
        )
        result = StepResult(
            "provision_reconciled",
            True,
            exec_result.message,
            ms,
            data={
                "steps_completed": exec_result.steps_completed,
                "created_service_port_indices": sorted(
                    {
                        int(entry.resource_id)
                        for entry in exec_result.compensation_log
                        if entry.step_name.startswith("create_service_port_")
                        and entry.resource_id is not None
                        and str(entry.resource_id).isdigit()
                    }
                ),
                **get_delta_summary(delta),
            },
        )
    else:
        logger.error(
            "Reconciled provisioning failed for ONT %s: %s",
            ctx.ont.serial_number,
            exec_result.message,
        )

        # Attempt rollback if there are compensation entries
        rollback_results = []
        if exec_result.compensation_log:
            logger.info(
                "Initiating rollback for ONT %s (%d compensation entries)",
                ctx.ont.serial_number,
                len(exec_result.compensation_log),
            )
            rollback_results = exec_result.rollback(ctx.olt)

        result = StepResult(
            "provision_reconciled",
            False,
            exec_result.message,
            ms,
            data={
                "steps_completed": exec_result.steps_completed,
                "steps_failed": exec_result.steps_failed,
                "errors": exec_result.errors,
                "rollback_performed": len(rollback_results) > 0,
                "rollback_results": [
                    {"step": r[0], "success": r[1], "message": r[2]}
                    for r in rollback_results
                ],
            },
        )

    _record_step(db, ctx.ont, "provision_reconciled", result)

    # Send operator notifications for provisioning events
    try:
        olt_name = getattr(ctx.olt, "name", None) or "OLT"
        if result.success:
            broadcast_websocket(
                event_type="ont_provisioning_success",
                title="ONT Provisioning Successful",
                message=f"ONT {ctx.ont.serial_number} provisioned on {olt_name} port {ctx.fsp}",
                metadata={
                    "ont_id": ont_id,
                    "serial_number": ctx.ont.serial_number,
                    "olt_name": olt_name,
                    "fsp": ctx.fsp,
                    "duration_ms": result.duration_ms,
                    "steps_completed": len(exec_result.steps_completed),
                },
            )
        else:
            result_data = result.data or {}
            notify.alert_operators(
                title="ONT Provisioning Failed",
                message=f"ONT {ctx.ont.serial_number} provisioning failed on {olt_name}: {result.message}",
                severity="error",
                metadata={
                    "ont_id": ont_id,
                    "serial_number": ctx.ont.serial_number,
                    "olt_name": olt_name,
                    "fsp": ctx.fsp,
                    "steps_failed": result_data.get("steps_failed", []),
                    "errors": result_data.get("errors", []),
                    "rollback_performed": result_data.get("rollback_performed", False),
                },
            )
    except Exception as notify_exc:
        logger.warning(
            "Failed to send provisioning notification for ONT %s: %s",
            ctx.ont.serial_number,
            notify_exc,
        )

    return result


def preview_reconciliation(
    db: Session,
    ont_id: str,
) -> dict:
    """Preview what reconciliation would do without executing.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.

    Returns:
        Dictionary with delta summary and validation results.
    """
    from app.services.network.ont_provisioning.reconciler import (
        get_delta_summary,
        reconcile_ont_state,
    )

    delta, err = reconcile_ont_state(
        db,
        ont_id,
    )
    if not delta:
        return {"error": err, "has_changes": False, "is_valid": False}

    summary = get_delta_summary(delta)
    summary["error"] = None

    # Add detail about each service port action
    port_details: list[dict] = []
    for sp_delta in delta.service_port_deltas:
        detail: dict = {
            "action": sp_delta.action.value,
            "message": sp_delta.message,
        }
        if sp_delta.desired:
            detail["desired"] = {
                "vlan_id": sp_delta.desired.vlan_id,
                "gem_index": sp_delta.desired.gem_index,
                "tag_transform": sp_delta.desired.tag_transform,
            }
        if sp_delta.actual:
            detail["actual"] = {
                "index": sp_delta.actual.index,
                "vlan_id": sp_delta.actual.vlan_id,
                "gem_index": sp_delta.actual.gem_index,
            }
        port_details.append(detail)
    summary["service_port_details"] = port_details

    return summary
