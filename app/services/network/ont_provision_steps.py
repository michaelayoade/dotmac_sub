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
- ``resolve_profile`` — profile resolution (explicit > assigned > default)
- ``validate_prerequisites`` — preflight checklist before provisioning
- ``preview_commands`` — dry-run OLT command generation
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.network import OntUnit
from app.services.network._common import NasTarget
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
from app.services.network.ont_provisioning.preview import (
    preview_commands as preview_commands,
)
from app.services.network.ont_provisioning.profiles import (
    resolve_profile as resolve_profile,
)
from app.services.network.ont_provisioning.result import StepResult as StepResult

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
    get_pppoe_push_max_attempts,
    get_pppoe_push_retry_delay,
    get_tr069_bootstrap_poll_interval,
    get_tr069_bootstrap_timeout,
)

_BOOTSTRAP_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_timeout_sec
_BOOTSTRAP_POLL_INTERVAL_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_poll_interval_sec
_TR069_TASK_READY_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_task_ready_timeout_sec
_TR069_TASK_READY_POLL_INTERVAL_SEC = (
    _PROVISIONING_DEFAULTS.tr069_task_ready_poll_interval_sec
)
_PPPOE_PUSH_MAX_ATTEMPTS = _PROVISIONING_DEFAULTS.pppoe_push_max_attempts
_PPPOE_PUSH_RETRY_DELAY_SEC = _PROVISIONING_DEFAULTS.pppoe_push_retry_delay_sec


# ---------------------------------------------------------------------------
# Step completion tracking
# ---------------------------------------------------------------------------


def _record_step(db: Session, ont: OntUnit, step_name: str, result: StepResult) -> None:
    """Log provisioning step completion on the ONT.

    The step data is logged for observability. The ONT's provisioning_status
    is updated when the overall workflow completes, not per-step.
    """
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
            (Vlan.olt_device_id == ctx.olt.id) | (Vlan.olt_device_id.is_(None)),
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
    result = StepResult("create_service_port", success, message, ms)
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
    from app.services.network.olt_ssh_ont import configure_ont_internet_config

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("activate_internet_config", False, err, critical=False)

    ok, msg = configure_ont_internet_config(
        ctx.olt, ctx.fsp, ctx.olt_ont_id, ip_index=ip_index
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult("activate_internet_config", ok, msg, ms, critical=False)
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
    from app.services.network.olt_ssh_ont import configure_ont_wan_config

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("configure_wan_olt", False, err, critical=False)

    ok, msg = configure_ont_wan_config(
        ctx.olt,
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
        profile_id=profile_id,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult("configure_wan_olt", ok, msg, ms, critical=False)
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
    from app.services.network.olt_ssh_ont import bind_tr069_server_profile

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
    ok, msg = bind_tr069_server_profile(
        ctx.olt, ctx.fsp, ctx.olt_ont_id, tr069_olt_profile_id
    )
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


def wait_tr069_bootstrap(
    db: Session,
    ont_id: str,
    *,
    allow_blocking: bool = False,
) -> StepResult:
    """Poll GenieACS until the ONT registers after TR-069 binding.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        allow_blocking: If True, skip the background context check. Use only
            when you explicitly want blocking behavior (e.g., in tests).

    Raises:
        BlockingOperationError: If called from a web request context without
            allow_blocking=True. Use queue_wait_tr069_bootstrap() instead.

    Warning:
        This function blocks with time.sleep() for up to 120 seconds.
        Call only from a Celery task or background thread, never from
        a web request handler.
    """
    # Guard against accidental blocking in web request handlers
    if not allow_blocking and not _is_background_context():
        raise BlockingOperationError(
            "wait_tr069_bootstrap() blocks for up to 120 seconds and should not be "
            "called from a web request handler. Use queue_wait_tr069_bootstrap() "
            "to run this operation in the background, or pass allow_blocking=True "
            "if you explicitly need blocking behavior."
        )

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return StepResult("wait_tr069_bootstrap", False, "ONT not found")

    # Get configurable timeouts from DomainSettings (or use defaults)
    bootstrap_timeout = get_tr069_bootstrap_timeout(db)
    poll_interval = get_tr069_bootstrap_poll_interval(db)

    try:
        from app.services.network._resolve import resolve_genieacs_with_reason

        logger.info(
            "TR-069 bootstrap wait started: ont_id=%s serial=%s timeout_sec=%s poll_interval_sec=%s",
            ont.id,
            ont.serial_number,
            bootstrap_timeout,
            poll_interval,
        )
        deadline = time.monotonic() + bootstrap_timeout
        attempt = 0
        last_poll_error = ""
        while time.monotonic() < deadline:
            attempt += 1
            try:
                resolved, reason = resolve_genieacs_with_reason(db, ont)
            except Exception as exc:
                db.rollback()
                last_poll_error = str(exc)
                logger.warning(
                    "TR-069 bootstrap wait poll error: ont_id=%s serial=%s attempt=%s error=%s",
                    ont.id,
                    ont.serial_number,
                    attempt,
                    exc,
                )
                time.sleep(poll_interval)
                continue
            if resolved:
                _client, device_id = resolved
                logger.info(
                    "TR-069 bootstrap complete: ont_id=%s serial=%s genieacs_device_id=%s attempts=%s",
                    ont.id,
                    ont.serial_number,
                    device_id,
                    attempt,
                )
                ms = int((time.monotonic() - t0) * 1000)
                result = StepResult(
                    "wait_tr069_bootstrap", True, "Device registered in ACS", ms
                )
                _record_step(db, ont, "wait_tr069_bootstrap", result)
                return result
            logger.info(
                "TR-069 bootstrap wait poll miss: ont_id=%s serial=%s attempt=%s reason=%s",
                ont.id,
                ont.serial_number,
                attempt,
                reason,
            )
            time.sleep(poll_interval)

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


def queue_wait_tr069_bootstrap(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
) -> StepResult:
    """Queue TR-069 bootstrap polling in the background."""
    from app.models.network_operation import (
        NetworkOperationTargetType,
        NetworkOperationType,
    )
    from app.services.network_operations import network_operations

    op = None
    try:
        op = network_operations.start(
            db,
            NetworkOperationType.tr069_bootstrap,
            NetworkOperationTargetType.ont,
            ont_id,
            correlation_key=f"tr069_bootstrap:{ont_id}",
            initiated_by=initiated_by,
        )
        network_operations.mark_waiting(
            db,
            str(op.id),
            "Waiting for background TR-069 bootstrap polling to start.",
        )
        db.commit()
        from app.celery_app import enqueue_celery_task

        enqueue_celery_task(
            "app.tasks.tr069.wait_for_ont_bootstrap",
            args=[ont_id, str(op.id)],
            correlation_id=f"tr069_bootstrap:{ont_id}",
            source="ont_provision_step",
        )
        return StepResult(
            "wait_tr069_bootstrap",
            False,
            f"Queued TR-069 bootstrap polling in the background (operation {op.id}).",
            critical=False,
            waiting=True,
            data={"operation_id": str(op.id)},
        )
    except Exception as exc:
        if op is not None:
            try:
                network_operations.mark_failed(
                    db,
                    str(op.id),
                    f"Failed to queue TR-069 bootstrap polling: {exc}",
                )
                db.commit()
            except Exception:
                logger.warning(
                    "Failed to mark bootstrap operation %s as failed after queue error",
                    getattr(op, "id", None),
                    exc_info=True,
                )
        return StepResult(
            "wait_tr069_bootstrap",
            False,
            f"Failed to queue TR-069 bootstrap polling: {exc}",
            critical=False,
        )


# ---------------------------------------------------------------------------
# SERVICE: Provision from WAN Service Instances
# ---------------------------------------------------------------------------


def _provision_wan_service_instances(
    db: Session,
    ont_id: str,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Provision TR-069 config from OntWanServiceInstance records.

    Returns:
        A tuple of (steps, needs_input, hard_failures) where:
        - steps: List of step results
        - needs_input: List of missing inputs
        - hard_failures: List of hard failure messages
    """
    from sqlalchemy import select

    from app.models.network import (
        OntProvisioningProfile,
        OntUnit,
        OntWanServiceInstance,
        WanConnectionType,
        WanServiceProvisioningStatus,
    )
    from app.services.credential_crypto import decrypt_credential
    from app.services.network.olt_ssh_ont import (
        configure_ont_internet_config,
        configure_ont_pppoe_omci,
        configure_ont_wan_config,
    )
    from app.services.network.ont_action_network import (
        configure_wan_config,
        set_pppoe_credentials,
    )

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return [], [], ["ONT not found"]

    # Query WAN service instances for this ONT
    instances = list(
        db.scalars(
            select(OntWanServiceInstance)
            .where(OntWanServiceInstance.ont_id == ont.id)
            .where(OntWanServiceInstance.is_active.is_(True))
            .order_by(OntWanServiceInstance.priority)
        ).all()
    )

    if not instances:
        return [], [], []

    profile = getattr(ont, "provisioning_profile", None)
    if profile is None and getattr(ont, "provisioning_profile_id", None):
        profile = db.get(OntProvisioningProfile, ont.provisioning_profile_id)

    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []
    olt_context = None

    def _resolve_olt_context_once():
        nonlocal olt_context
        if olt_context is not None:
            return olt_context, None
        ctx, err = resolve_olt_context(db, ont_id)
        if ctx is None:
            return None, err
        olt_context = ctx
        return ctx, None

    def _append(name: str, result) -> None:
        success = bool(getattr(result, "success", False))
        message = str(getattr(result, "message", ""))
        steps.append(
            {
                "step": name,
                "success": success,
                "waiting": bool(getattr(result, "waiting", False)),
                "message": message,
            }
        )
        if not success and not getattr(result, "waiting", False):
            hard_failures.append(f"{name}: {message}")

    def _append_step(
        name: str, success: bool, message: str, *, hard_failure: bool = True
    ) -> None:
        steps.append(
            {
                "step": name,
                "success": success,
                "waiting": False,
                "message": message,
            }
        )
        if not success and hard_failure:
            hard_failures.append(f"{name}: {message}")

    def _provision_pppoe_omci(
        instance,
        service_label: str,
        wan_vlan: int | None,
    ) -> tuple[bool, bool]:
        """Attempt PPPoE provisioning via OLT OMCI commands.

        Returns:
            Tuple of (attempted, succeeded):
            - attempted: True if OMCI provisioning was attempted
            - succeeded: True if OMCI provisioning completed successfully

        When attempted=False, the caller should try TR-069 as an alternative.
        When attempted=True but succeeded=False, the caller may still try
        TR-069 as a fallback.
        """
        # Check provisioning method preference
        pppoe_method = get_pppoe_provisioning_method(db)
        if pppoe_method == "tr069":
            # Operator configured TR-069 only - skip OMCI entirely
            logger.info(
                "PPPoE provisioning method set to 'tr069' for ONT %s, "
                "skipping OMCI provisioning",
                ont_id,
            )
            return (False, False)

        wan_profile_id = int(getattr(profile, "wan_config_profile_id", None) or 0)
        omci_vlan = getattr(profile, "pppoe_omci_vlan", None)
        if omci_vlan is None:
            omci_vlan = wan_vlan
        if omci_vlan is None:
            # No VLAN configured for OMCI - can't attempt, try TR-069
            return (False, False)
        if not get_olt_write_mode_enabled(db):
            # OLT write mode disabled - skip OMCI, try TR-069 instead
            logger.info(
                "OLT write mode disabled for ONT %s, skipping OMCI provisioning",
                ont_id,
            )
            return (False, False)

        pppoe_username = instance.pppoe_username
        pppoe_password = None
        if instance.pppoe_password:
            try:
                pppoe_password = decrypt_credential(instance.pppoe_password)
            except Exception:
                pppoe_password = None
        if not pppoe_username or not pppoe_password:
            needs_input.append(
                f"PPPoE credentials missing for WAN service '{service_label}'."
            )
            # Missing credentials - can't proceed with either method
            return (True, False)

        ctx, err = _resolve_olt_context_once()
        if ctx is None:
            # No OLT context - skip OMCI, try TR-069 instead
            logger.warning(
                "OLT context not found for ONT %s: %s - will try TR-069 fallback",
                ont_id,
                err,
            )
            return (False, False)

        profile_ip_index = getattr(profile, "internet_config_ip_index", None)
        ip_index = (
            int(profile_ip_index)
            if profile_ip_index is not None
            else int(instance.priority or 1)
        )

        # Step 1: Activate TCP stack on ONT via internet-config
        inet_ok, inet_msg = configure_ont_internet_config(
            ctx.olt,
            ctx.fsp,
            ctx.olt_ont_id,
            ip_index=ip_index,
        )
        _append_step(
            f"internet_config_olt:{service_label}",
            inet_ok,
            inet_msg,
            hard_failure=False,
        )
        if not inet_ok:
            # internet-config failure is non-fatal; some ONTs don't need it
            logger.warning(
                "internet-config failed for ONT %s ip-index %d: %s",
                ont_id,
                ip_index,
                inet_msg,
            )

        # Step 2: Set route+NAT mode via wan-config when this OLT supports
        # ONT WAN profiles. Some MA56xx builds support PPPoE ipconfig but not
        # `ont wan-profile`; those profiles keep wan_config_profile_id empty.
        if wan_profile_id:
            wan_ok, wan_msg = configure_ont_wan_config(
                ctx.olt,
                ctx.fsp,
                ctx.olt_ont_id,
                ip_index=ip_index,
                profile_id=wan_profile_id,
            )
            _append_step(f"configure_wan_olt:{service_label}", wan_ok, wan_msg)
            if not wan_ok:
                # wan-config failed - fall through to TR-069
                logger.warning(
                    "wan-config failed for ONT %s: %s - will try TR-069 fallback",
                    ont_id,
                    wan_msg,
                )
                return (True, False)

        pppoe_ok, pppoe_msg = configure_ont_pppoe_omci(
            ctx.olt,
            ctx.fsp,
            ctx.olt_ont_id,
            ip_index=ip_index,
            vlan_id=int(omci_vlan),
            priority=int(getattr(instance, "cos_priority", None) or 0),
            username=str(pppoe_username),
            password=str(pppoe_password),
        )
        _append_step(
            f"configure_pppoe_omci:{service_label}",
            pppoe_ok,
            pppoe_msg,
            hard_failure=False,  # Don't mark as hard failure; TR-069 may succeed
        )
        if pppoe_ok:
            from datetime import UTC, datetime

            instance.provisioning_status = WanServiceProvisioningStatus.provisioned
            instance.last_provisioned_at = datetime.now(UTC)
            instance.last_error = None
            return (True, True)
        else:
            # OMCI PPPoE config failed - fall through to TR-069
            logger.warning(
                "OMCI PPPoE config failed for ONT %s: %s - will try TR-069 fallback",
                ont_id,
                pppoe_msg,
            )
            return (True, False)

    def _igd_wan_instance_for_vlan(wan_vlan: int | None) -> int | None:
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
        for item in connections:
            if not isinstance(item, dict):
                continue
            service = str(item.get("detected_wan_service") or "").upper()
            vlan = str(item.get("detected_wan_vlan") or "").strip()
            if service == "TR069":
                continue
            if requested_vlan and vlan == requested_vlan:
                return int(item.get("index") or 0) or None
        for item in connections:
            if not isinstance(item, dict):
                continue
            service = str(item.get("detected_wan_service") or "").upper()
            vlan = str(item.get("detected_wan_vlan") or "").strip()
            ppp_entries = int(item.get("ppp_entries") or 0)
            if service == "TR069" or (
                requested_vlan and vlan and vlan != requested_vlan
            ):
                continue
            if ppp_entries > 0:
                return int(item.get("index") or 0) or None
        for item in connections:
            if not isinstance(item, dict):
                continue
            service = str(item.get("detected_wan_service") or "").upper()
            vlan = str(item.get("detected_wan_vlan") or "").strip()
            ip_entries = int(item.get("ip_entries") or 0)
            ppp_entries = int(item.get("ppp_entries") or 0)
            if service == "TR069" or (
                requested_vlan and vlan and vlan != requested_vlan
            ):
                continue
            if ip_entries == 0 and ppp_entries == 0:
                return int(item.get("index") or 0) or None
        return None

    def _mark_ppp_wan_requires_precreated() -> None:
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

    for idx, instance in enumerate(instances, start=1):
        service_label = instance.name or instance.service_type.value
        if str(instance.service_type.value) == "management":
            steps.append(
                {
                    "step": f"configure_wan_tr069:{service_label}",
                    "success": True,
                    "waiting": False,
                    "message": "Skipped: management WAN is configured on the OLT.",
                }
            )
            continue
        logger.info(
            "Provisioning WAN service instance %d/%d (%s) for ONT %s",
            idx,
            len(instances),
            service_label,
            ont.serial_number,
        )

        # Determine WAN mode from connection_type
        wan_mode_map = {
            WanConnectionType.pppoe: "pppoe",
            WanConnectionType.dhcp: "dhcp",
            WanConnectionType.static: "static",
            WanConnectionType.bridged: "bridge",
        }
        wan_mode = wan_mode_map.get(instance.connection_type, "pppoe")

        # Get VLAN tag
        wan_vlan = instance.s_vlan
        if wan_vlan is None and instance.vlan:
            wan_vlan = instance.vlan.tag

        # Try OMCI provisioning first for PPPoE; fall through to TR-069 if it fails
        # unless pppoe_provisioning_method is set to "omci" (OMCI-only mode)
        pppoe_method = get_pppoe_provisioning_method(db)
        if wan_mode == "pppoe":
            omci_attempted, omci_succeeded = _provision_pppoe_omci(
                instance,
                service_label,
                wan_vlan,
            )
            if omci_succeeded:
                # OMCI succeeded - no need to try TR-069
                continue
            if omci_attempted:
                if pppoe_method == "omci":
                    # OMCI-only mode: don't fall back to TR-069
                    logger.warning(
                        "OMCI provisioning failed for ONT %s service %s, "
                        "and pppoe_provisioning_method=omci prevents TR-069 fallback",
                        ont.serial_number,
                        service_label,
                    )
                    continue
                logger.info(
                    "OMCI provisioning failed for ONT %s service %s, "
                    "attempting TR-069 fallback",
                    ont.serial_number,
                    service_label,
                )

        acs_instance_index = idx
        if (
            wan_mode == "pppoe"
            and getattr(ont, "tr069_data_model", None) == "InternetGatewayDevice"
        ):
            detected_index = _igd_wan_instance_for_vlan(wan_vlan)
            if detected_index is None:
                message = (
                    f"No safe WANConnectionDevice exists for PPPoE VLAN {wan_vlan}. "
                    "The ONT must expose or precreate an internet PPP WAN container "
                    "before TR-069 can push credentials."
                )
                steps.append(
                    {
                        "step": f"configure_wan_tr069:{service_label}",
                        "success": False,
                        "waiting": False,
                        "message": message,
                    }
                )
                hard_failures.append(f"configure_wan_tr069:{service_label}: {message}")
                from app.models.network import WanServiceProvisioningStatus

                instance.provisioning_status = WanServiceProvisioningStatus.failed
                instance.last_error = message[:500]
                _mark_ppp_wan_requires_precreated()
                continue
            acs_instance_index = detected_index

        # Configure WAN mode (creates WAN service if needed)
        if wan_mode:
            wan_result = configure_wan_config(
                db,
                ont_id,
                wan_mode=wan_mode,
                wan_vlan=wan_vlan,
                instance_index=acs_instance_index,
            )
            _append(f"configure_wan_tr069:{service_label}", wan_result)

        # Push PPPoE credentials if applicable
        if instance.connection_type == WanConnectionType.pppoe:
            pppoe_username = instance.pppoe_username
            pppoe_password = None
            if instance.pppoe_password:
                try:
                    pppoe_password = decrypt_credential(instance.pppoe_password)
                except Exception:
                    pppoe_password = None

            if pppoe_username and pppoe_password:
                pppoe_result = set_pppoe_credentials(
                    db,
                    ont_id,
                    pppoe_username,
                    pppoe_password,
                    wan_vlan=wan_vlan,
                    instance_index=acs_instance_index,
                )
                _append(f"push_pppoe_tr069:{service_label}", pppoe_result)

                # Update instance status on success
                if pppoe_result.success:
                    from datetime import UTC, datetime

                    from app.models.network import WanServiceProvisioningStatus

                    instance.provisioning_status = (
                        WanServiceProvisioningStatus.provisioned
                    )
                    instance.last_provisioned_at = datetime.now(UTC)
                    instance.last_error = None
                elif getattr(pppoe_result, "waiting", False):
                    from app.models.network import WanServiceProvisioningStatus

                    instance.provisioning_status = WanServiceProvisioningStatus.pending
                    instance.last_error = (
                        pppoe_result.message[:500] if pppoe_result.message else None
                    )
                else:
                    from app.models.network import WanServiceProvisioningStatus

                    instance.provisioning_status = WanServiceProvisioningStatus.failed
                    instance.last_error = (
                        pppoe_result.message[:500] if pppoe_result.message else None
                    )
            else:
                needs_input.append(
                    f"PPPoE credentials missing for WAN service '{service_label}'."
                )

    return steps, needs_input, hard_failures


def apply_saved_service_config(db: Session, ont_id: str) -> StepResult:
    """Apply saved TR-069 service intent once the ONT is visible in ACS.

    This function first checks for OntWanServiceInstance records (Phase 2+3
    architecture). If found, it provisions from those instances which provide
    grouped L2/L3 configuration with resolved credentials.

    If no WAN service instances exist, falls back to the legacy flat-field
    approach using ont_plan and OntUnit fields for backward compatibility.

    Missing operator inputs are reported in ``data["needs_input"]`` and do not
    fail the bootstrap operation; they can be supplied later and retried.
    """
    from app.services.credential_crypto import decrypt_credential
    from app.services.network.ont_action_network import (
        configure_wan_config,
        probe_wan_capabilities,
        set_lan_config,
        set_pppoe_credentials,
    )
    from app.services.network.ont_action_network import (
        set_connection_request_credentials as set_cr_credentials,
    )
    from app.services.network.ont_action_wifi import set_wifi_config
    from app.services.network.ont_service_intent import load_ont_plan_for_ont

    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if ont is None:
        return StepResult("apply_saved_service_config", False, "ONT not found")

    ont_plan: dict[str, object] = load_ont_plan_for_ont(db, ont_id=ont_id) or {}
    steps: list[dict[str, object]] = []
    needs_input: list[str] = []
    hard_failures: list[str] = []
    waiting = False

    def _section(name: str) -> dict[str, object]:
        value = ont_plan.get(name)
        return value if isinstance(value, dict) else {}

    def _optional_int(value: object) -> int | None:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else None

    def _append(name: str, result) -> None:
        nonlocal waiting
        waiting = waiting or bool(getattr(result, "waiting", False))
        success = bool(getattr(result, "success", False))
        message = str(getattr(result, "message", ""))
        steps.append(
            {
                "step": name,
                "success": success,
                "waiting": bool(getattr(result, "waiting", False)),
                "message": message,
            }
        )
        if not success and not getattr(result, "waiting", False):
            hard_failures.append(f"{name}: {message}")

    bind_plan = _section("bind_tr069")
    profile = getattr(ont, "provisioning_profile", None)
    if profile is None and getattr(ont, "provisioning_profile_id", None):
        from app.models.network import OntProvisioningProfile

        profile = db.get(OntProvisioningProfile, ont.provisioning_profile_id)
    cr_username = getattr(profile, "cr_username", None) if profile else None
    cr_password = getattr(profile, "cr_password", None) if profile else None
    if cr_password:
        cr_password = decrypt_credential(cr_password) or str(cr_password)
    if cr_username and cr_password:
        _append(
            "set_connection_request_credentials",
            set_cr_credentials(db, ont_id, str(cr_username), str(cr_password)),
        )
    elif bind_plan:
        needs_input.append("Connection request credentials are incomplete.")

    probe_result = probe_wan_capabilities(db, ont_id)
    _append("probe_wan_capabilities", probe_result)

    # Phase 2+3: Check for WAN service instances first
    # If instances exist, use them for WAN/PPPoE provisioning (grouped L2/L3)
    wan_instance_steps, wan_instance_needs, wan_instance_failures = (
        _provision_wan_service_instances(db, ont_id)
    )
    use_wan_instances = bool(wan_instance_steps or wan_instance_needs)

    if use_wan_instances:
        # Use WAN service instances (Phase 2+3 architecture)
        for step in wan_instance_steps:
            steps.append(step)
            if step.get("waiting"):
                waiting = True
        needs_input.extend(wan_instance_needs)
        hard_failures.extend(wan_instance_failures)
        logger.info(
            "ONT %s: Provisioned %d WAN service instance steps",
            ont.serial_number,
            len(wan_instance_steps),
        )
    else:
        # Legacy fallback: use flat fields and ont_plan
        wan_plan = _section("configure_wan_tr069")
        ont_wan_mode = getattr(ont, "wan_mode", None)
        wan_mode = wan_plan.get("wan_mode") or (
            ont_wan_mode.value if ont_wan_mode is not None else None
        )
        if wan_mode == "static_ip":
            wan_mode = "static"
        if wan_mode:
            wan_vlan = wan_plan.get("wan_vlan")
            if wan_vlan is None and getattr(ont, "wan_vlan", None) is not None:
                wan_vlan = getattr(ont.wan_vlan, "tag", None)
            _append(
                "configure_wan_tr069",
                configure_wan_config(
                    db,
                    ont_id,
                    wan_mode=str(wan_mode),
                    wan_vlan=_optional_int(wan_vlan),
                    ip_address=str(wan_plan.get("ip_address") or "") or None,
                    subnet_mask=str(wan_plan.get("subnet_mask") or "") or None,
                    gateway=str(wan_plan.get("gateway") or "") or None,
                    dns_servers=str(wan_plan.get("dns_servers") or "") or None,
                ),
            )

        pppoe_plan = _section("push_pppoe_tr069")
        pppoe_username = (
            pppoe_plan.get("username")
            or pppoe_plan.get("pppoe_username")
            or getattr(ont, "pppoe_username", None)
        )
        pppoe_password = (
            decrypt_credential(ont.pppoe_password)
            if getattr(ont, "pppoe_password", None)
            else None
        )
        if pppoe_username and pppoe_password:
            # Pass wan_vlan so PPP WAN service can be auto-created if missing
            pppoe_wan_vlan = _optional_int(wan_vlan)
            _append(
                "push_pppoe_tr069",
                set_pppoe_credentials(
                    db,
                    ont_id,
                    str(pppoe_username),
                    str(pppoe_password),
                    wan_vlan=pppoe_wan_vlan,
                ),
            )
        elif pppoe_plan or wan_mode == "pppoe":
            needs_input.append("PPPoE username and password are required.")

    lan_plan = _section("configure_lan_tr069")
    lan_values = {
        "lan_ip": getattr(ont, "lan_gateway_ip", None) or lan_plan.get("lan_ip"),
        "lan_subnet": getattr(ont, "lan_subnet_mask", None)
        or lan_plan.get("lan_subnet"),
        "dhcp_enabled": getattr(ont, "lan_dhcp_enabled", None)
        if getattr(ont, "lan_dhcp_enabled", None) is not None
        else lan_plan.get("dhcp_enabled"),
        "dhcp_start": getattr(ont, "lan_dhcp_start", None)
        or lan_plan.get("dhcp_start"),
        "dhcp_end": getattr(ont, "lan_dhcp_end", None) or lan_plan.get("dhcp_end"),
    }
    if any(value not in (None, "", []) for value in lan_values.values()):
        dhcp_enabled_value = lan_values.get("dhcp_enabled")
        _append(
            "configure_lan_tr069",
            set_lan_config(
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

    wifi_plan = _section("configure_wifi_tr069")
    wifi_password = (
        decrypt_credential(ont.wifi_password)
        if getattr(ont, "wifi_password", None)
        else None
    )
    channel = getattr(ont, "wifi_channel", None) or wifi_plan.get("channel")
    try:
        channel_int = int(str(channel).strip()) if channel not in (None, "") else None
    except (TypeError, ValueError):
        channel_int = None
        needs_input.append("WiFi channel must be numeric.")
    wifi_values = {
        "enabled": getattr(ont, "wifi_enabled", None)
        if getattr(ont, "wifi_enabled", None) is not None
        else wifi_plan.get("enabled"),
        "ssid": getattr(ont, "wifi_ssid", None) or wifi_plan.get("ssid"),
        "password": wifi_password,
        "channel": channel_int,
        "security_mode": getattr(ont, "wifi_security_mode", None)
        or wifi_plan.get("security_mode"),
    }
    if any(value not in (None, "", []) for value in wifi_values.values()):
        wifi_enabled_value = wifi_values.get("enabled")
        _append(
            "configure_wifi_tr069",
            set_wifi_config(
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
    elif wifi_plan.get("password_set"):
        needs_input.append(
            "WiFi password was requested but no saved password is available."
        )

    ms = int((time.monotonic() - t0) * 1000)
    if hard_failures:
        return StepResult(
            "apply_saved_service_config",
            False,
            "; ".join(hard_failures),
            ms,
            critical=False,
            waiting=waiting,
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
        message += " Some inputs are still required."
    return StepResult(
        "apply_saved_service_config",
        True,
        message,
        ms,
        critical=False,
        waiting=waiting,
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
    from app.services.network.ont_action_network import (
        set_connection_request_credentials as _set_cr,
    )

    t0 = time.monotonic()
    cr_result = _set_cr(db, ont_id, username, password)
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
    from app.services.network.olt_ssh_ont import configure_ont_pppoe_omci

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("push_pppoe_omci", False, err)

    ok, msg = configure_ont_pppoe_omci(
        ctx.olt,
        ctx.fsp,
        ctx.olt_ont_id,
        ip_index=ip_index,
        vlan_id=vlan_id,
        priority=priority,
        username=username,
        password=password,
    )
    ms = int((time.monotonic() - t0) * 1000)
    unsupported = not ok and _is_unsupported_omci_command(msg)
    if unsupported:
        msg += " (unsupported)"
    result = StepResult("push_pppoe_omci", ok, msg, ms)
    _record_step(db, ctx.ont, "push_pppoe_omci", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Push PPPoE credentials via TR-069
# ---------------------------------------------------------------------------


def push_pppoe_tr069(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    instance_index: int = 1,
    retry: bool = True,
) -> StepResult:
    """Push PPPoE credentials to ONT via TR-069/ACS.

    Includes task reachability wait and retry logic.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        username: PPPoE username.
        password: PPPoE password.
        instance_index: WAN instance index (default 1, must be 1-8).
        retry: Whether to retry on failure (default True).
    """
    from app.services.network.ont_action_network import (
        set_pppoe_credentials as _set_pppoe,
    )

    t0 = time.monotonic()

    # Validate instance_index early
    if instance_index < 1 or instance_index > 8:
        return StepResult(
            "push_pppoe_tr069",
            False,
            f"Invalid WAN instance index: {instance_index} (must be 1-8)",
            0,
        )
    # Get configurable retry settings from DomainSettings (or use defaults)
    max_attempts = get_pppoe_push_max_attempts(db) if retry else 1
    retry_delay = get_pppoe_push_retry_delay(db)
    last_result = None

    for attempt in range(1, max_attempts + 1):
        last_result = _set_pppoe(
            db, ont_id, username, password, instance_index=instance_index
        )
        if last_result.success or getattr(last_result, "waiting", False):
            break
        if attempt >= max_attempts:
            break
        logger.info(
            "Retrying PPPoE push for ONT %s (attempt %d): %s",
            ont_id,
            attempt,
            last_result.message,
        )
        time.sleep(retry_delay)

    ms = int((time.monotonic() - t0) * 1000)
    if last_result is None:
        return StepResult(
            "push_pppoe_tr069", False, "No result from PPPoE push attempts", ms
        )
    result = StepResult(
        "push_pppoe_tr069",
        last_result.success,
        last_result.message,
        ms,
        waiting=getattr(last_result, "waiting", False),
        data=getattr(last_result, "data", None),
    )
    ont = db.get(OntUnit, ont_id)
    if ont:
        _record_step(db, ont, "push_pppoe_tr069", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Configure WAN mode via TR-069
# ---------------------------------------------------------------------------


def configure_wan_tr069(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str = "pppoe",
    wan_vlan: int | str | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: str | None = None,
    instance_index: int = 1,
) -> StepResult:
    """Configure WAN connection mode on ONT via TR-069.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        wan_mode: WAN mode — "pppoe", "dhcp", "static", or "bridge".
        wan_vlan: WAN VLAN ID (optional).
        ip_address: Static WAN IP address when wan_mode is "static".
        subnet_mask: Static WAN subnet mask when wan_mode is "static".
        gateway: Static WAN gateway when wan_mode is "static".
        dns_servers: Comma-separated DNS servers when wan_mode is "static".
        instance_index: WAN instance index (default 1).
    """
    from app.services.network.ont_action_network import configure_wan_config

    t0 = time.monotonic()
    resolved_wan_vlan = (
        int(wan_vlan)
        if isinstance(wan_vlan, str) and wan_vlan.strip().isdigit()
        else wan_vlan
    )
    if isinstance(resolved_wan_vlan, str):
        resolved_wan_vlan = None

    action_result = configure_wan_config(
        db,
        ont_id,
        wan_mode=wan_mode,
        wan_vlan=resolved_wan_vlan,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "configure_wan_tr069",
        action_result.success,
        action_result.message,
        ms,
        critical=False,
        waiting=getattr(action_result, "waiting", False),
        data=getattr(action_result, "data", None),
    )
    ont = db.get(OntUnit, ont_id)
    if ont:
        _record_step(db, ont, "configure_wan_tr069", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Enable IPv6 dual-stack via TR-069
# ---------------------------------------------------------------------------


def enable_ipv6(
    db: Session,
    ont_id: str,
    *,
    wan_instance: int = 1,
) -> StepResult:
    """Enable IPv6 dual-stack on the ONT WAN interface via TR-069.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        wan_instance: WAN instance index (default 1).
    """
    t0 = time.monotonic()
    try:
        from app.services.network.ont_action_network import enable_ipv6_on_wan

        v6_result = enable_ipv6_on_wan(db, ont_id, wan_instance=wan_instance)
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "enable_ipv6", v6_result.success, v6_result.message, ms, critical=False
        )
        ont = db.get(OntUnit, ont_id)
        if ont:
            _record_step(db, ont, "enable_ipv6", result)
        return result
    except Exception as exc:
        logger.error("IPv6 enable failed for ONT %s: %s", ont_id, exc)
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "enable_ipv6", False, f"IPv6 enable failed: {exc}", ms, critical=False
        )
        ont = db.get(OntUnit, ont_id)
        if ont:
            try:
                _record_step(db, ont, "enable_ipv6", result)
            except Exception:
                logger.debug(
                    "Failed to record enable_ipv6 step after IPv6 enable error",
                    exc_info=True,
                )
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
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    t0 = time.monotonic()
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("rollback_service_ports", False, err)

    ok, _msg, ports = get_service_ports_for_ont(ctx.olt, ctx.fsp, ctx.olt_ont_id)
    if not ok or not ports:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "rollback_service_ports", True, "No service ports to remove", ms
        )

    deleted = 0
    errors = 0
    for port in ports:
        ok, msg = delete_service_port(ctx.olt, port.index)
        if ok:
            deleted += 1
        else:
            errors += 1
            logger.warning(
                "Rollback: failed to delete service-port %d: %s", port.index, msg
            )

    ms = int((time.monotonic() - t0) * 1000)
    message = f"Removed {deleted} service-port(s)"
    if errors:
        message += f", {errors} failed"
    result = StepResult("rollback_service_ports", errors == 0, message, ms)
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
    if getattr(ont, "mgmt_ip_address", None):
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
    ont.mgmt_ip_address = selected_ip

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
    profile_id: str | None = None,
    tr069_olt_profile_id: int | None = None,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
) -> StepResult:
    """Provision an ONT using state reconciliation.

    This is the recommended approach for ONT provisioning. It:
    1. Builds desired state from the profile (no reference cloning)
    2. Reads actual state from the OLT (single SSH session)
    3. Computes delta (existing matching ports = NOOP = idempotent)
    4. Validates (optical budget, VLAN trunk, ip_index bounds)
    5. Executes with compensation log (rollback on failure)

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        profile_id: Optional explicit profile ID.
        tr069_olt_profile_id: Optional explicit OLT-local TR-069 profile ID.
        dry_run: If True, compute delta but don't execute.
        allow_low_optical_margin: If True, proceed even with low optical margin.

    Returns:
        StepResult with provisioning outcome.
    """
    from app.services.network.ont_provisioning.executor import execute_delta
    from app.services.network.ont_provisioning.reconciler import (
        get_delta_summary,
        reconcile_ont_state,
    )
    from app.services.network.ont_provisioning.state import (
        build_desired_state_from_profile,
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

    profile = resolve_profile(db, ctx.ont, profile_id)
    static_ip_ok, static_ip_msg = _ensure_static_management_ip_from_profile(
        db, ctx.ont, profile
    )
    if not static_ip_ok:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult("provision_reconciled", False, static_ip_msg, ms)

    # Reconcile state
    delta, err = reconcile_ont_state(
        db,
        ont_id,
        profile_id,
        tr069_olt_profile_id=tr069_olt_profile_id,
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
    desired, err = build_desired_state_from_profile(
        db,
        ont_id,
        tr069_olt_profile_id=tr069_olt_profile_id,
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

    # Execute the delta
    exec_result = execute_delta(ctx.olt, delta, desired)

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
    return result


def preview_reconciliation(
    db: Session,
    ont_id: str,
    *,
    profile_id: str | None = None,
    tr069_olt_profile_id: int | None = None,
) -> dict:
    """Preview what reconciliation would do without executing.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        profile_id: Optional explicit profile ID.

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
        profile_id,
        tr069_olt_profile_id=tr069_olt_profile_id,
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
