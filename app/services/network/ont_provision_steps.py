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
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningProfile,
    OntUnit,
    PonPort,
)
from app.services.common import coerce_uuid
from app.services.network.serial_utils import (
    parse_ont_id_on_olt as _parse_ont_id_on_olt,
)

logger = logging.getLogger(__name__)

# Bootstrap polling constants (patchable in tests)
_BOOTSTRAP_TIMEOUT_SEC = 120
_BOOTSTRAP_POLL_INTERVAL_SEC = 10
_TR069_TASK_READY_TIMEOUT_SEC = 45
_TR069_TASK_READY_POLL_INTERVAL_SEC = 5
_PPPOE_PUSH_MAX_ATTEMPTS = 3
_PPPOE_PUSH_RETRY_DELAY_SEC = 10


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


def _json_safe_step_data(value: Any) -> Any:
    """Normalize StepResult data to JSON-safe primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_step_data(asdict(value))
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _json_safe_step_data(value.model_dump())
    if hasattr(value, "_asdict") and callable(value._asdict):
        return _json_safe_step_data(value._asdict())
    if isinstance(value, Mapping):
        items = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        return {key: _json_safe_step_data(item) for key, item in items}
    if isinstance(value, set):
        normalized_items = [_json_safe_step_data(item) for item in value]
        return sorted(normalized_items, key=lambda item: repr(item))
    if isinstance(value, (list, tuple)):
        return [_json_safe_step_data(item) for item in value]
    return str(value)


@dataclass
class StepResult:
    """Result of a single provisioning operation."""

    step_name: str
    success: bool
    message: str
    duration_ms: int = 0
    critical: bool = True
    skipped: bool = False
    waiting: bool = False
    data: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.data is not None:
            normalized = _json_safe_step_data(self.data)
            self.data = (
                normalized if isinstance(normalized, dict) else {"value": normalized}
            )


# ---------------------------------------------------------------------------
# ONT → OLT context resolution (shared by all services)
# ---------------------------------------------------------------------------


@dataclass
class OltContext:
    """Resolved ONT-to-OLT mapping needed for SSH operations."""

    ont: OntUnit
    olt: OLTDevice
    fsp: str
    olt_ont_id: int
    assignment: OntAssignment | None = None


def resolve_olt_context(db: Session, ont_id: str) -> tuple[OltContext | None, str]:
    """Resolve ONT → OLT + FSP + ONT-ID for SSH operations.

    Returns:
        (context, error_message). Context is None on failure.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, "ONT not found"

    assignment: OntAssignment | None = None
    for a in getattr(ont, "assignments", []):
        if a.active:
            assignment = a
            break
    if not assignment:
        return None, "ONT has no active assignment"
    if not assignment.pon_port_id:
        return None, "Assignment has no PON port"

    pon_port: PonPort | None = db.get(PonPort, str(assignment.pon_port_id))
    if not pon_port:
        return None, "PON port not found"

    olt: OLTDevice | None = db.get(OLTDevice, str(pon_port.olt_id))
    if not olt:
        return None, "OLT not found"

    board = ont.board or ""
    port = ont.port or ""
    if board and port:
        fsp = f"{board}/{port}"
    elif pon_port.name:
        fsp = pon_port.name
    else:
        return None, "Cannot determine F/S/P"

    olt_ont_id = _parse_ont_id_on_olt(ont.external_id)
    if olt_ont_id is None:
        return None, f"No usable ONT-ID in external_id ({ont.external_id!r})"

    return OltContext(
        ont=ont, olt=olt, fsp=fsp, olt_ont_id=olt_ont_id, assignment=assignment
    ), ""


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


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


_CREDENTIAL_KEYWORDS = ("password", "secret", "Password")


def mask_credentials(cmd: str) -> str:
    """Mask credential values in OLT CLI command strings for safe logging."""
    for kw in _CREDENTIAL_KEYWORDS:
        idx = cmd.find(f" {kw} ")
        if idx != -1:
            prefix = cmd[: idx + len(kw) + 2]
            rest = cmd[idx + len(kw) + 2 :]
            next_space = rest.find(" ")
            if next_space == -1:
                cmd = prefix + "********"
            else:
                cmd = prefix + "********" + rest[next_space:]
    return cmd


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
        mgmt_vlan_id=str(vlan_id),
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

    ok, msg = bind_tr069_server_profile(
        ctx.olt, ctx.fsp, ctx.olt_ont_id, tr069_olt_profile_id
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult("bind_tr069", ok, msg, ms)
    _record_step(db, ctx.ont, "bind_tr069", result)
    return result


# ---------------------------------------------------------------------------
# SERVICE: Wait for TR-069 bootstrap (GenieACS registration)
# ---------------------------------------------------------------------------


def wait_tr069_bootstrap(
    db: Session,
    ont_id: str,
) -> StepResult:
    """Poll GenieACS until the ONT registers after TR-069 binding.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.

    Warning:
        This function blocks with time.sleep() for up to 120 seconds.
        Call only from a Celery task or background thread, never from
        a web request handler.
    """
    t0 = time.monotonic()
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return StepResult("wait_tr069_bootstrap", False, "ONT not found")

    try:
        from app.services.network._resolve import resolve_genieacs_with_reason

        deadline = time.monotonic() + _BOOTSTRAP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            resolved, reason = resolve_genieacs_with_reason(db, ont)
            if resolved:
                logger.info("TR-069 bootstrap complete for ONT %s", ont.serial_number)
                ms = int((time.monotonic() - t0) * 1000)
                result = StepResult(
                    "wait_tr069_bootstrap", True, "Device registered in ACS", ms
                )
                _record_step(db, ont, "wait_tr069_bootstrap", result)
                return result
            time.sleep(_BOOTSTRAP_POLL_INTERVAL_SEC)

        logger.warning(
            "TR-069 bootstrap timeout for ONT %s after %ds",
            ont.serial_number,
            _BOOTSTRAP_TIMEOUT_SEC,
        )
        ms = int((time.monotonic() - t0) * 1000)
        result = StepResult(
            "wait_tr069_bootstrap",
            False,
            f"Device not found in ACS after {_BOOTSTRAP_TIMEOUT_SEC}s",
            ms,
        )
        _record_step(db, ont, "wait_tr069_bootstrap", result)
        return result
    except Exception as e:
        logger.error("Error during TR-069 bootstrap poll: %s", e)
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
        celery_app.send_task(
            "app.tasks.tr069.wait_for_ont_bootstrap",
            args=[ont_id, str(op.id)],
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
    max_attempts = _PPPOE_PUSH_MAX_ATTEMPTS if retry else 1
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
        time.sleep(_PPPOE_PUSH_RETRY_DELAY_SEC)

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
    t0 = time.monotonic()

    # TR-069 WAN mode configuration is not yet implemented as an
    # ont_action_network action.  Log the intent and return a waiting result
    # so callers know the step was attempted but deferred.
    logger.info(
        "configure_wan_tr069 requested for ONT %s: wan_mode=%s wan_vlan=%s "
        "ip_address=%s instance_index=%s (not yet implemented)",
        ont_id,
        wan_mode,
        wan_vlan,
        ip_address,
        instance_index,
    )
    ms = int((time.monotonic() - t0) * 1000)
    result = StepResult(
        "configure_wan_tr069",
        False,
        "TR-069 WAN mode configuration is not yet implemented.",
        ms,
        waiting=True,
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


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def resolve_profile(
    db: Session,
    ont: OntUnit,
    profile_id: str | None = None,
) -> OntProvisioningProfile | None:
    """Resolve a provisioning profile for an ONT.

    Priority: explicit profile_id > ONT's assigned profile > first active profile.
    """
    selected_id = profile_id or (
        str(ont.provisioning_profile_id) if ont.provisioning_profile_id else None
    )
    if selected_id:
        return db.get(OntProvisioningProfile, selected_id)
    fallback = db.scalars(
        select(OntProvisioningProfile).where(OntProvisioningProfile.is_active.is_(True))
    ).first()
    if fallback:
        logger.warning(
            "ONT %s has no assigned profile — falling back to '%s'",
            ont.serial_number,
            fallback.name,
        )
    return fallback


def _profile_requires_tr069(profile: OntProvisioningProfile | None) -> bool:
    """Check whether a profile's configuration requires TR-069."""
    if profile is None:
        return False
    if getattr(profile, "cr_username", None) or getattr(profile, "cr_password", None):
        return True
    if getattr(getattr(profile, "ip_protocol", None), "value", None) == "dual_stack":
        return True

    wan_services = getattr(profile, "wan_services", []) or []
    has_pppoe = any(
        (
            getattr(getattr(ws, "connection_type", None), "value", None)
            or str(getattr(ws, "connection_type", "") or "")
        )
        == "pppoe"
        for ws in wan_services
        if getattr(ws, "is_active", True)
    )
    omci_vlan = getattr(profile, "pppoe_omci_vlan", None)
    return has_pppoe and omci_vlan is None


# ---------------------------------------------------------------------------
# Preflight validation
# ---------------------------------------------------------------------------


def validate_prerequisites(
    db: Session,
    ont_id: str,
    *,
    profile_id: str | None = None,
    tr069_olt_profile_id: int | None = None,
) -> dict:
    """Check all prerequisites before provisioning.

    Returns a dict with:
    - ready: bool — all checks pass
    - checks: list of {name, status, message, can_auto_fix}
    """
    from app.models.catalog import (
        AccessCredential,
        Subscription,
        SubscriptionStatus,
    )

    checks: list[dict] = []
    ont = db.get(OntUnit, coerce_uuid(ont_id))
    olt: OLTDevice | None = None
    profile: OntProvisioningProfile | None = None

    if not ont:
        checks.append(
            {
                "name": "ONT exists",
                "status": "fail",
                "message": "ONT not found",
                "can_auto_fix": False,
            }
        )
        return {"ready": False, "checks": checks}
    checks.append(
        {
            "name": "ONT exists",
            "status": "ok",
            "message": f"{ont.serial_number} ({ont.vendor or ''} {ont.model or ''})",
            "can_auto_fix": False,
        }
    )

    if ont.olt_device_id:
        olt = db.get(OLTDevice, ont.olt_device_id)
        checks.append(
            {
                "name": "OLT assigned",
                "status": "ok",
                "message": olt.name if olt else str(ont.olt_device_id),
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "OLT assigned",
                "status": "fail",
                "message": "No OLT — assign ONT to an OLT first",
                "can_auto_fix": False,
            }
        )

    if ont.board and ont.port is not None:
        checks.append(
            {
                "name": "OLT position (F/S/P)",
                "status": "ok",
                "message": f"{ont.board}/{ont.port}",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "OLT position (F/S/P)",
                "status": "fail",
                "message": "Board/port not set — discover from OLT or enter manually",
                "can_auto_fix": False,
            }
        )

    profile = resolve_profile(db, ont, profile_id)
    if profile:
        checks.append(
            {
                "name": "Provisioning profile",
                "status": "ok",
                "message": profile.name,
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "Provisioning profile",
                "status": "fail",
                "message": "No profile — create one in Catalog → Provisioning Profiles",
                "can_auto_fix": False,
            }
        )

    if olt and olt.ssh_username and olt.ssh_password:
        checks.append(
            {
                "name": "OLT SSH credentials",
                "status": "ok",
                "message": f"User: {olt.ssh_username}",
                "can_auto_fix": False,
            }
        )
    elif ont.olt_device_id:
        checks.append(
            {
                "name": "OLT SSH credentials",
                "status": "fail",
                "message": "SSH not configured on OLT",
                "can_auto_fix": False,
            }
        )

    assignment = db.scalars(
        select(OntAssignment).where(
            OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True)
        )
    ).first()
    if assignment and assignment.subscriber_id:
        from app.models.subscriber import Subscriber

        sub = db.get(Subscriber, assignment.subscriber_id)
        sub_name = (
            f"{sub.first_name or ''} {sub.last_name or ''}".strip()
            if sub
            else str(assignment.subscriber_id)
        )
        checks.append(
            {
                "name": "Subscriber assigned",
                "status": "ok",
                "message": sub_name,
                "can_auto_fix": False,
            }
        )

        active_sub = db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id == assignment.subscriber_id,
                Subscription.status == SubscriptionStatus.active,
            )
        ).first()
        if active_sub:
            checks.append(
                {
                    "name": "Active subscription",
                    "status": "ok",
                    "message": str(active_sub.id)[:8] + "...",
                    "can_auto_fix": False,
                }
            )
        else:
            checks.append(
                {
                    "name": "Active subscription",
                    "status": "warn",
                    "message": "No active subscription",
                    "can_auto_fix": False,
                }
            )

        cred = db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscriber_id == assignment.subscriber_id,
                AccessCredential.is_active.is_(True),
            )
        ).first()
        if cred:
            checks.append(
                {
                    "name": "PPPoE credential",
                    "status": "ok",
                    "message": cred.username,
                    "can_auto_fix": False,
                }
            )
        else:
            checks.append(
                {
                    "name": "PPPoE credential",
                    "status": "warn",
                    "message": "None — will auto-generate on activation",
                    "can_auto_fix": True,
                }
            )
    else:
        checks.append(
            {
                "name": "Subscriber assigned",
                "status": "warn",
                "message": "No subscriber — provisioning will skip PPPoE",
                "can_auto_fix": False,
            }
        )

    acs_server_id = ont.tr069_acs_server_id
    if not acs_server_id and olt is not None:
        acs_server_id = getattr(olt, "tr069_acs_server_id", None)
    if acs_server_id:
        checks.append(
            {
                "name": "TR-069 ACS server",
                "status": "ok",
                "message": "Configured",
                "can_auto_fix": False,
            }
        )
    else:
        checks.append(
            {
                "name": "TR-069 ACS server",
                "status": "warn",
                "message": "Not configured — TR-069 steps will be skipped",
                "can_auto_fix": False,
            }
        )

    profile_requires = _profile_requires_tr069(profile)
    acs_enabled = bool(
        getattr(ont, "tr069_acs_server_id", None)
        or getattr(olt, "tr069_acs_server_id", None)
    )
    tr069_required = profile_requires or acs_enabled

    if not tr069_required or tr069_olt_profile_id is not None:
        tr069_status = "ok"
        tr069_msg = "Configured"
    elif profile_requires and not acs_enabled:
        tr069_status = "fail"
        tr069_msg = "Selected provisioning profile requires TR-069, but no ACS-enabled OLT or ONT is configured."
    elif profile_requires:
        tr069_status = "fail"
        tr069_msg = "Selected provisioning profile requires a TR-069 OLT profile ID."
    else:
        tr069_status = "fail"
        tr069_msg = (
            "This ONT is on an ACS-enabled OLT. Provide a TR-069 OLT profile ID."
        )
    checks.append(
        {
            "name": "TR-069 OLT profile",
            "status": tr069_status,
            "message": tr069_msg,
            "can_auto_fix": False,
        }
    )

    ready = all(c["status"] != "fail" for c in checks)
    return {"ready": ready, "checks": checks}


# ---------------------------------------------------------------------------
# Command preview (dry-run)
# ---------------------------------------------------------------------------


def preview_commands(
    db: Session,
    ont_id: str,
    profile_id: str,
    *,
    tr069_olt_profile_id: int | None = None,
) -> dict[str, Any]:
    """Generate provisioning commands without executing them.

    Returns:
        Dict with keys: success, message, command_sets (list of OltCommandSet dicts).
    """
    from app.services.network.olt_command_gen import (
        HuaweiCommandGenerator,
        OntProvisioningContext,
        build_spec_from_profile,
    )

    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return {"success": False, "message": err, "command_sets": []}

    profile = db.get(OntProvisioningProfile, profile_id)
    if not profile:
        return {"success": False, "message": "Profile not found", "command_sets": []}

    fsp_parts = ctx.fsp.split("/")
    if len(fsp_parts) < 3:
        return {
            "success": False,
            "message": f"FSP '{ctx.fsp}' needs 3 segments (frame/slot/port)",
            "command_sets": [],
        }
    prov_ctx = OntProvisioningContext(
        frame=int(fsp_parts[0]),
        slot=int(fsp_parts[1]),
        port=int(fsp_parts[2]),
        ont_id=ctx.olt_ont_id,
        olt_name=ctx.olt.name,
    )

    spec = build_spec_from_profile(
        profile, prov_ctx, tr069_profile_id=tr069_olt_profile_id
    )
    command_sets = HuaweiCommandGenerator.generate_full_provisioning(spec, prov_ctx)

    return {
        "success": True,
        "message": f"Generated {sum(len(cs.commands) for cs in command_sets)} command(s)",
        "command_sets": [
            {
                "step": cs.step,
                "commands": [mask_credentials(c) for c in cs.commands],
                "description": cs.description,
            }
            for cs in command_sets
        ],
    }
