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

from sqlalchemy.orm import Session

from app.models.network import OntUnit
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
    get_pppoe_push_max_attempts,
    get_pppoe_push_retry_delay,
    get_tr069_bootstrap_poll_interval,
    get_tr069_bootstrap_timeout,
)

_BOOTSTRAP_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_timeout_sec
_BOOTSTRAP_POLL_INTERVAL_SEC = _PROVISIONING_DEFAULTS.tr069_bootstrap_poll_interval_sec
_TR069_TASK_READY_TIMEOUT_SEC = _PROVISIONING_DEFAULTS.tr069_task_ready_timeout_sec
_TR069_TASK_READY_POLL_INTERVAL_SEC = _PROVISIONING_DEFAULTS.tr069_task_ready_poll_interval_sec
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
) -> StepResult:
    """Create a single L2 service-port VLAN/GEM binding on the OLT.

    Validates the VLAN exists in the system before sending the SSH command,
    then delegates to ``ont_write.update_service_port`` for SSH + DB persistence.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        vlan_id: VLAN to bind.
        gem_index: GEM port index (default 1).
        user_vlan: User-side VLAN (optional).
        tag_transform: Tag transform mode (default "translate").
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
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "create_service_port", action_result.success, action_result.message, ms
    )
    _record_step(db, ctx.ont, "create_service_port", result)
    return result


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
        instance_index: WAN instance index (default 1).
        retry: Whether to retry on failure (default True).
    """
    from app.services.network.ont_action_network import (
        set_pppoe_credentials as _set_pppoe,
    )

    t0 = time.monotonic()
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
    nas_device_id: str,
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
        nas_device_id: NAS device primary key.
        vlan_id: VLAN ID to create.
        parent_interface: Physical interface (default "ether3").
        ip_address: IP address with CIDR for the VLAN interface.
        pppoe_service_name: Optional PPPoE service name.
        pppoe_default_profile: PPP profile name (default "default").
    """
    from app.models.catalog import NasDevice

    t0 = time.monotonic()
    nas = db.get(NasDevice, nas_device_id)
    if not nas:
        return StepResult(
            "ensure_nas_vlan", False, f"NAS device {nas_device_id} not found"
        )

    try:
        from app.services.nas._mikrotik_vlan import provision_vlan_full

        vlan_result = provision_vlan_full(
            nas,
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
