"""ONT provisioning services — direct config application.

RECOMMENDED APPROACH:
    provision_with_reconciliation(db, ont_id)

This function reads config from the source of truth (OntAssignment + OltConfigPack)
and applies it directly to the OLT. The OLT adapter handles idempotency by treating
"already exists" errors as success.

The direct approach:
1. Reads effective config via resolve_effective_ont_config()
2. Creates internet service port (adapter handles "already exists")
3. Executes batched management config (service port, IPHOST, TR-069)
4. No state reconciliation needed - adapter layer is idempotent

Other functions in this module:
- wait_tr069_bootstrap() — Poll for ACS registration after TR-069 binding
- apply_saved_service_config() — Apply TR-069 service config after bootstrap
- deprovision() — Remove service-ports and return ONT to inventory
- rollback_service_ports() — Cleanup on provisioning failure
- preview_provisioning() — Show what config would be applied
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.network import OntUnit
from app.services.network._common import NasTarget
from app.services.network.effective_ont_config import resolve_effective_ont_config
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


# ---------------------------------------------------------------------------
# NOTE: Individual step functions for manual wizard provisioning have been
# removed. The following functions were deprecated:
#   - create_service_port() - Use provision_with_reconciliation() instead
#   - configure_management_ip() - Use provision_with_reconciliation() instead
#   - activate_internet_config() - Use provision_with_reconciliation() instead
#   - configure_wan_olt() - Use provision_with_reconciliation() instead
#   - bind_tr069() - Use provision_with_reconciliation() instead
#
# All OLT provisioning now uses direct config application:
# provision_with_reconciliation() reads from OntAssignment + OltConfigPack
# (source of truth) and applies config directly. The OLT adapter handles
# idempotency by treating "already exists" errors as success.
# ---------------------------------------------------------------------------


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
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_action_wan import (
        set_pppoe_credentials,
        set_wan_dhcp,
        set_wan_static,
    )

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return [], [], ["ONT not found"]

    effective = resolve_effective_ont_config(db, ont)
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    wan_mode = str(effective_values.get("wan_mode") or "").strip().lower()
    wan_vlan = effective_values.get("wan_vlan")
    wan_vlan_int = int(wan_vlan) if wan_vlan not in (None, "") else None
    # Use OLT-derived WCD index as default for PPPoE (from config pack)
    pppoe_wcd_default = effective_values.get("pppoe_wcd_index") or 2
    instance_index = int(effective_values.get("wan_instance_index") or pppoe_wcd_default)
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
# NOTE: Additional deprecated step functions:
#   - set_connection_request_credentials() - Applied via apply_saved_service_config()
#   - push_pppoe_omci() - Applied via _provision_wan_service_instances()
#
# These are now called internally from apply_saved_service_config() which
# reads config from the source of truth (OntAssignment + OltConfigPack).
# ---------------------------------------------------------------------------


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
    # Source of truth is the effective config (OntAssignment + OltConfigPack)
    effective_mgmt_ip = effective_values.get("mgmt_ip_address")
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
    # Store the allocated IP on the active assignment (source of truth)
    assignment = effective.get("assignment")
    if assignment is not None:
        assignment.mgmt_ip_address = selected_ip

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
# DIRECT CONFIG-BASED PROVISIONING
# ---------------------------------------------------------------------------


def provision_with_reconciliation(
    db: Session,
    ont_id: str,
    *,
    dry_run: bool = False,
    allow_low_optical_margin: bool = False,
) -> StepResult:
    """Provision an ONT by applying config directly. Adapter handles idempotency.

    This simplified approach:
    1. Reads effective config via resolve_effective_ont_config()
    2. Creates internet service port (adapter treats "already exists" as success)
    3. Executes batched management config (mgmt port, IPHOST, TR-069)

    No state reconciliation needed - the OLT adapter layer is idempotent.

    Args:
        db: Database session.
        ont_id: OntUnit primary key.
        dry_run: If True, show what would be configured without executing.
        allow_low_optical_margin: Ignored (kept for API compatibility).

    Returns:
        StepResult with provisioning outcome.
    """
    from app.services.network.olt_batched_mgmt import (
        create_batched_mgmt_spec_from_config_pack,
    )
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    t0 = time.monotonic()

    # Get context
    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return StepResult("provision", False, err)

    # Resolve config from source of truth
    effective = resolve_effective_ont_config(db, ctx.ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    config_pack = effective.get("config_pack")

    if not config_pack:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult("provision", False, "OLT config pack not found", ms)

    logger.info(
        "Starting provisioning for ONT %s serial=%s olt=%s fsp=%s",
        ont_id,
        ctx.ont.serial_number,
        ctx.olt.name,
        ctx.fsp,
    )

    # Build preview data
    wan_vlan = values.get("wan_vlan")
    wan_gem = int(values.get("wan_gem_index") or 1)
    mgmt_vlan = values.get("mgmt_vlan")
    tr069_profile = values.get("tr069_olt_profile_id")

    if dry_run:
        ms = int((time.monotonic() - t0) * 1000)
        return StepResult(
            "provision",
            True,
            "Dry run: showing config to apply",
            ms,
            data={
                "dry_run": True,
                "wan_vlan": wan_vlan,
                "wan_gem_index": wan_gem,
                "mgmt_vlan": mgmt_vlan,
                "tr069_profile_id": tr069_profile,
                "mgmt_ip_mode": values.get("mgmt_ip_mode"),
            },
        )

    adapter = get_protocol_adapter(ctx.olt)
    steps_completed: list[str] = []
    created_port_indices: list[int] = []

    # 1. Create internet service port (adapter returns success on "already exists")
    if wan_vlan:
        result = adapter.create_service_port(
            ctx.fsp,
            ctx.olt_ont_id,
            gem_index=wan_gem,
            vlan_id=int(wan_vlan),
        )
        if not result.success:
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                f"Internet service port failed: {result.message}",
                ms,
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        steps_completed.append(f"service_port_vlan_{wan_vlan}")
        if result.data and result.data.get("service_port_index"):
            created_port_indices.append(result.data["service_port_index"])

    # 2. Execute batched management config (mgmt port, IPHOST, internet-config, wan-config, TR-069)
    if mgmt_vlan:
        mgmt_spec = create_batched_mgmt_spec_from_config_pack(
            config_pack,
            ctx.fsp,
            ctx.olt_ont_id,
            allocated_ip=values.get("mgmt_ip_address"),
            subnet_mask=values.get("mgmt_subnet"),
            gateway=values.get("mgmt_gateway"),
        )
        result = adapter.configure_management_batch(mgmt_spec)
        if not result.success:
            # Rollback created ports on failure
            for port_idx in created_port_indices:
                try:
                    adapter.delete_service_port(port_idx)
                except Exception:
                    pass
            ms = int((time.monotonic() - t0) * 1000)
            step_result = StepResult(
                "provision",
                False,
                f"Management config failed: {result.message}",
                ms,
                data={"rollback_performed": len(created_port_indices) > 0},
            )
            _record_step(db, ctx.ont, "provision", step_result)
            _send_failure_notification(ctx, ont_id, step_result)
            return step_result
        steps_completed.extend(result.data.get("steps_completed", []))

    ms = int((time.monotonic() - t0) * 1000)
    step_result = StepResult(
        "provision",
        True,
        f"Provisioned: {len(steps_completed)} step(s)",
        ms,
        data={
            "steps_completed": steps_completed,
            "created_service_port_indices": created_port_indices,
        },
    )
    _record_step(db, ctx.ont, "provision", step_result)

    # Send success notification
    try:
        olt_name = getattr(ctx.olt, "name", None) or "OLT"
        broadcast_websocket(
            event_type="ont_provisioning_success",
            title="ONT Provisioning Successful",
            message=f"ONT {ctx.ont.serial_number} provisioned on {olt_name} port {ctx.fsp}",
            metadata={
                "ont_id": ont_id,
                "serial_number": ctx.ont.serial_number,
                "olt_name": olt_name,
                "fsp": ctx.fsp,
                "duration_ms": step_result.duration_ms,
                "steps_completed": len(steps_completed),
            },
        )
    except Exception as notify_exc:
        logger.warning(
            "Failed to send provisioning notification for ONT %s: %s",
            ctx.ont.serial_number,
            notify_exc,
        )

    return step_result


def _send_failure_notification(ctx: OltContext, ont_id: str, result: StepResult) -> None:
    """Send failure notification to operators."""
    try:
        olt_name = getattr(ctx.olt, "name", None) or "OLT"
        notify.alert_operators(
            title="ONT Provisioning Failed",
            message=f"ONT {ctx.ont.serial_number} provisioning failed on {olt_name}: {result.message}",
            severity="error",
            metadata={
                "ont_id": ont_id,
                "serial_number": ctx.ont.serial_number,
                "olt_name": olt_name,
                "fsp": ctx.fsp,
            },
        )
    except Exception as notify_exc:
        logger.warning(
            "Failed to send failure notification for ONT %s: %s",
            ctx.ont.serial_number,
            notify_exc,
        )


def preview_reconciliation(
    db: Session,
    ont_id: str,
) -> dict:
    """Preview what provisioning would configure (no OLT state reading).

    Args:
        db: Database session.
        ont_id: OntUnit primary key.

    Returns:
        Dictionary with config that would be applied.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return {"error": "ONT not found", "has_changes": False, "is_valid": False}

    ctx, err = resolve_olt_context(db, ont_id)
    if not ctx:
        return {"error": err, "has_changes": False, "is_valid": False}

    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}
    config_pack = effective.get("config_pack")

    if not config_pack:
        return {"error": "OLT config pack not found", "has_changes": False, "is_valid": False}

    wan_vlan = values.get("wan_vlan")
    mgmt_vlan = values.get("mgmt_vlan")

    # Build service port list
    service_ports = []
    if wan_vlan:
        service_ports.append({
            "purpose": "internet",
            "vlan_id": int(wan_vlan),
            "gem_index": int(values.get("wan_gem_index") or 1),
        })
    if mgmt_vlan:
        service_ports.append({
            "purpose": "management",
            "vlan_id": int(mgmt_vlan),
            "gem_index": int(values.get("mgmt_gem_index") or 2),
        })

    return {
        "error": None,
        "has_changes": len(service_ports) > 0,
        "is_valid": True,
        "service_ports": service_ports,
        "management": {
            "vlan": mgmt_vlan,
            "ip_mode": values.get("mgmt_ip_mode"),
            "ip_address": values.get("mgmt_ip_address"),
        } if mgmt_vlan else None,
        "tr069": {
            "profile_id": values.get("tr069_olt_profile_id"),
            "acs_server_id": values.get("tr069_acs_server_id"),
        } if values.get("tr069_olt_profile_id") else None,
        "internet_config_ip_index": values.get("internet_config_ip_index"),
        "wan_config_profile_id": values.get("wan_config_profile_id"),
    }
