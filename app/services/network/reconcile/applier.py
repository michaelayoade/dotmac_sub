"""apply_plan(plan, ctx) — execute the planner's action list in order.

Takes a ``Plan`` and an ``ApplyContext`` (the I/O dependencies: OLT SSH
adapter, GenieACS NBI client, secret resolver) and runs each action via a
``match``-dispatched executor. Halts on first hard failure; records every
``AppliedAction`` regardless of outcome; returns an ``ApplyResult`` describing
what happened.

Action → adapter call mapping:

    OLT actions
      OltAuthorize              → adapter.authorize_ont
      OltModifyDescription      → adapter.set_ont_description
      OltModifyLineProfile      → adapter.update_ont_profiles (line only)
      OltModifyServiceProfile   → adapter.update_ont_profiles (srv only)
      OltClearIphost            → adapter.clear_iphost_config
      OltIpconfig               → adapter.configure_iphost (mode=static)
      OltTr069ServerConfig      → adapter.bind_tr069_profile
      OltCreateServicePort      → adapter.create_service_port
      OltDeleteServicePort      → adapter.delete_service_port
      OltOmciPppoe              → adapter.configure_pppoe
      OltOmciInternetConfig     → adapter.configure_internet_config
      OltOmciWanConfig          → adapter.configure_wan_config
      OltReset                  → adapter.reboot_ont

    ACS actions
      AcsAddObject              → client.add_object
      AcsSetPppoe               → client.set_parameter_values (6 params)
      AcsSetWifiSsid            → client.set_parameter_values (1 param)
      AcsSetWifiPassword        → client.set_parameter_values (1 param)
      AcsSetNatEnabled          → client.set_parameter_values (1 param)
      AcsSetDhcpServer          → client.set_parameter_values (4 params)
      AcsSetManagementServer    → client.set_parameter_values (3 params)

Failure modes mapped to ``ReconcileFailureReason``:
* Adapter ``OltOperationResult.success=False``           → OLT_WRITE_REJECTED
* NBI 202 with ``connectionRequestError`` populated      → ACS_CR_FAILED
* NBI 5xx / ``GenieACSError``                            → ACS_WRITE_FAULTED
* NBI fault key set on the task (CWMP 9xxx)              → ACS_WRITE_FAULTED
* Apply pass exceeds ``deadline``                        → TIMEOUT
* Action type without a wired executor                   → OLT_WRITE_REJECTED
                                                            (with NotImplemented
                                                            in error message)

Secrets are resolved at execute time via ``ctx.resolve_secret(ref)``. The
applier never logs the resolved plaintext — callers configure logging on
the adapter/client themselves, but neither this module nor the action
``__repr__``s should ever surface a password.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .actions import (
    AcsAction,
    AcsAddObject,
    AcsSetDhcpServer,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetWifiPassword,
    AcsSetWifiSsid,
    Action,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
)
from .planner import Plan
from .state import AppliedAction, ReconcileFailure, ReconcileFailureReason

logger = logging.getLogger(__name__)


SecretResolver = Callable[[str], str]
"""Resolves a secret reference (e.g. ``bao://path/to/secret``) to plaintext.

The default identity resolver treats the ref string AS the plaintext — useful
for transitional periods before OpenBao is wired everywhere. Tests substitute
their own resolver.
"""


def passthrough_secret(ref: str) -> str:
    """Identity secret resolver. Treats the ref as the plaintext value."""
    return ref


@dataclass
class ApplyContext:
    """I/O dependencies for ``apply_plan``.

    ``olt_adapter`` is an ``OltProtocolAdapter`` (or any object with the
    same method surface). ``acs_client`` is a ``GenieACSClient``. Both can
    be substituted in tests. ``resolve_secret`` defaults to passthrough.
    """

    olt_adapter: Any
    acs_client: Any
    resolve_secret: SecretResolver = passthrough_secret


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of ``apply_plan``.

    ``actions_applied`` lists every action up to and including the failing
    one (if any). ``halted_by`` is the failure that stopped the apply pass;
    None on success.
    """

    success: bool
    actions_applied: tuple[AppliedAction, ...]
    halted_by: ReconcileFailure | None


class ApplyError(Exception):
    """One action's write failed. Carries the action + failure reason so the
    applier can attach it to the ``ApplyResult``."""

    def __init__(self, action: Action, reason: str, message: str):
        self.action = action
        self.reason = reason
        self.message = message
        super().__init__(f"{type(action).__name__}: {message}")


def apply_plan(
    plan: Plan,
    ctx: ApplyContext,
    *,
    deadline: datetime | None = None,
) -> ApplyResult:
    """Execute the plan's actions in order. Halt on first hard failure.

    The returned ``ApplyResult.actions_applied`` includes every successful
    write up to (but not including) the failing one. The failing action is
    described by ``halted_by.message``.

    A None deadline means "no apply-side cap" — reconcile_ont enforces the
    outer budget. When the deadline is exceeded between actions (checked
    before each call), the apply pass returns TIMEOUT with the actions
    applied so far.
    """
    applied: list[AppliedAction] = []

    for action in plan.actions:
        if deadline is not None and datetime.now(UTC) >= deadline:
            return ApplyResult(
                success=False,
                actions_applied=tuple(applied),
                halted_by=ReconcileFailure(
                    reason=ReconcileFailureReason.TIMEOUT,
                    message=(
                        f"apply deadline exceeded before "
                        f"{type(action).__name__}"
                    ),
                ),
            )
        try:
            applied.append(_execute(action, ctx))
        except ApplyError as exc:
            logger.warning(
                "reconcile_apply_halted",
                extra={
                    "action": type(action).__name__,
                    "reason": exc.reason,
                    # `message` is reserved by logging.LogRecord — use `detail`.
                    "detail": exc.message,
                },
            )
            return ApplyResult(
                success=False,
                actions_applied=tuple(applied),
                halted_by=ReconcileFailure(
                    reason=exc.reason, message=exc.message
                ),
            )

    return ApplyResult(
        success=True,
        actions_applied=tuple(applied),
        halted_by=None,
    )


# ── Per-action dispatch ─────────────────────────────────────────────────────


def _execute(action: Action, ctx: ApplyContext) -> AppliedAction:
    """Dispatch one action to the appropriate adapter/client call.

    Pattern-matched by type. Each arm:
      1. resolves any secret refs to plaintext
      2. calls the underlying writer
      3. translates the writer's success/failure into AppliedAction or
         ApplyError
    """
    started = time.monotonic()

    match action:
        # ── OLT actions ───────────────────────────────────────────────────
        case OltAuthorize():
            result = ctx.olt_adapter.authorize_ont(
                action.fsp,
                action.serial_number,
                line_profile_id=action.line_profile_id,
                service_profile_id=action.service_profile_id,
                description=action.description,
            )
            _olt_check(action, result)
            return _ok(action, "olt_authorize", None, action.serial_number, started)

        case OltModifyDescription():
            result = ctx.olt_adapter.set_ont_description(
                action.fsp,
                action.ont_id,
                action.description,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "olt_description",
                None,
                action.description,
                started,
            )

        case OltModifyLineProfile():
            result = ctx.olt_adapter.update_ont_profiles(
                action.fsp,
                action.ont_id,
                line_profile_id=action.line_profile_id,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "olt_line_profile_id",
                None,
                action.line_profile_id,
                started,
            )

        case OltModifyServiceProfile():
            result = ctx.olt_adapter.update_ont_profiles(
                action.fsp,
                action.ont_id,
                service_profile_id=action.service_profile_id,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "olt_service_profile_id",
                None,
                action.service_profile_id,
                started,
            )

        case OltClearIphost():
            result = ctx.olt_adapter.clear_iphost_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
            )
            _olt_check(action, result)
            return _ok(
                action,
                f"iphost[{action.ip_index}]",
                None,
                None,
                started,
            )

        case OltIpconfig():
            result = ctx.olt_adapter.configure_iphost(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                mode="static",
                vlan=action.vlan,
                priority=action.priority,
                ip_address=action.ip_address,
                subnet_mask=action.subnet_mask,
                gateway=action.gateway,
            )
            _olt_check(action, result)
            return _ok(
                action, "olt_mgmt_ip", None, action.ip_address, started
            )

        case OltTr069ServerConfig():
            result = ctx.olt_adapter.bind_tr069_profile(
                action.fsp,
                action.ont_id,
                profile_id=action.profile_id,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "olt_tr069_profile_id",
                None,
                action.profile_id,
                started,
            )

        case OltCreateServicePort():
            result = ctx.olt_adapter.create_service_port(
                action.fsp,
                action.ont_id,
                gem_index=action.gem_index,
                vlan_id=action.vlan,
                user_vlan=action.vlan,
                port_index=action.service_port_index,
            )
            _olt_check(action, result)
            return _ok(
                action,
                f"service_port[{action.slot}]",
                None,
                action.service_port_index,
                started,
            )

        case OltDeleteServicePort():
            result = ctx.olt_adapter.delete_service_port(
                action.service_port_index
            )
            _olt_check(action, result)
            return _ok(
                action,
                f"service_port[{action.service_port_index}]",
                action.service_port_index,
                None,
                started,
            )

        case OltOmciPppoe():
            password = _resolve_or_fail(ctx, action, action.password_ref)
            result = ctx.olt_adapter.configure_pppoe(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                vlan_id=action.vlan,
                username=action.username,
                password=password,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "omci_pppoe",
                None,
                action.username,
                started,
            )

        case OltOmciInternetConfig():
            result = ctx.olt_adapter.configure_internet_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "omci_internet_config",
                None,
                action.ip_index,
                started,
            )

        case OltOmciWanConfig():
            result = ctx.olt_adapter.configure_wan_config(
                action.fsp,
                action.ont_id,
                ip_index=action.ip_index,
                profile_id=action.profile_id,
            )
            _olt_check(action, result)
            return _ok(
                action,
                "omci_wan_config",
                None,
                action.profile_id,
                started,
            )

        case OltReset():
            result = ctx.olt_adapter.reboot_ont(
                action.fsp, action.ont_id
            )
            _olt_check(action, result)
            return _ok(action, "ont_reset", None, None, started)

        # ── ACS actions ───────────────────────────────────────────────────
        case AcsAddObject():
            try:
                ctx.acs_client.add_object(action.device_id, action.object_path)
            except Exception as exc:  # noqa: BLE001 — translate to ApplyError
                raise ApplyError(
                    action,
                    ReconcileFailureReason.ACS_WRITE_FAULTED,
                    f"addObject failed: {exc}",
                ) from exc
            return _ok(
                action,
                f"add_object[{action.object_path.split('.')[-1]}]",
                None,
                action.object_path,
                started,
            )

        case AcsSetPppoe():
            password = _resolve_or_fail(ctx, action, action.password_ref)
            params = {
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.Username": action.username,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.Password": password,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.Enable": True,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.ConnectionType": "IP_Routed",
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.X_HW_VLAN": action.vlan,
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.X_HW_SERVICELIST": "INTERNET",
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_pppoe", None, action.username, started)

        case AcsSetWifiSsid():
            params = {
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": (
                    action.ssid
                )
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_ssid", None, action.ssid, started)

        case AcsSetWifiPassword():
            password = _resolve_or_fail(ctx, action, action.password_ref)
            params = {
                "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase": password
            }
            _acs_set(action, ctx, params)
            # Don't include the password in the AppliedAction — log the field name only.
            return _ok(action, "acs_wifi_password", None, "[redacted]", started)

        case AcsSetNatEnabled():
            params = {
                f"InternetGatewayDevice.WANDevice.1.WANConnectionDevice.{action.wcd_index}.WANPPPConnection.{action.instance_index}.NATEnabled": action.enabled
            }
            _acs_set(action, ctx, params)
            return _ok(
                action, "acs_nat_enabled", None, action.enabled, started
            )

        case AcsSetDhcpServer():
            params = {
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable": action.enabled,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress": action.pool_min,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress": action.pool_max,
                "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.SubnetMask": action.subnet_mask,
            }
            _acs_set(action, ctx, params)
            return _ok(action, "acs_dhcp_server", None, action.enabled, started)

        case AcsSetManagementServer():
            cr_password = _resolve_or_fail(
                ctx, action, action.cr_password_ref
            )
            params = {
                "InternetGatewayDevice.ManagementServer.ConnectionRequestUsername": action.cr_username,
                "InternetGatewayDevice.ManagementServer.ConnectionRequestPassword": cr_password,
                "InternetGatewayDevice.ManagementServer.PeriodicInformInterval": action.inform_interval_sec,
            }
            _acs_set(action, ctx, params)
            return _ok(
                action,
                "acs_management_server",
                None,
                action.inform_interval_sec,
                started,
            )

        case _:
            raise ApplyError(
                action,
                ReconcileFailureReason.OLT_WRITE_REJECTED,
                f"no executor wired for action type {type(action).__name__}",
            )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_or_fail(ctx: ApplyContext, action: Action, ref: str) -> str:
    """Resolve a secret ref to plaintext via the context resolver.

    Any exception from the resolver (OpenBao 5xx, network timeout, missing
    field on the KV path, etc.) is translated to an ApplyError with
    ACS_WRITE_FAULTED so the operator sees the failing action and a clear
    message instead of an unhandled 500.

    Empty/None refs return an empty string — the action sites that handle
    optional secrets (rare) can still see the resolver completed cleanly.
    """
    if not ref:
        return ""
    try:
        value = ctx.resolve_secret(ref)
    except Exception as exc:  # noqa: BLE001 — translate to typed apply failure
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"secret resolution failed: {exc}",
        ) from exc
    if value is None:
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            "secret resolver returned None",
        )
    return value


def _olt_check(action: Action, result: Any) -> None:
    """Raise ApplyError if the adapter reported failure.

    Per ``OltProtocolAdapter``'s contract, all writes return
    ``OltOperationResult`` with a ``success`` boolean and a ``message``.
    """
    if not getattr(result, "success", False):
        raise ApplyError(
            action,
            ReconcileFailureReason.OLT_WRITE_REJECTED,
            str(getattr(result, "message", "OLT rejected the write")),
        )


def _acs_set(action: AcsAction, ctx: ApplyContext, params: dict) -> None:
    """Push a setParameterValues batch via the NBI client.

    Today's Fix #5 made ``set_parameter_values`` pass ``connection_request=True``
    by default; the response carries ``connectionRequestError`` populated when
    the task was queued but not delivered (typically empty CR creds on a
    fresh device). The applier treats that as ACS_CR_FAILED so reconcile_ont
    can map it to a clean 4xx/5xx for the operator. Fault payloads on the
    task (CWMP 9002 etc.) are translated to ACS_WRITE_FAULTED.
    """
    try:
        task = ctx.acs_client.set_parameter_values(
            action.device_id,  # all ACS actions carry device_id
            params,
        )
    except Exception as exc:  # noqa: BLE001
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"setParameterValues failed: {exc}",
        ) from exc

    cr_error = ""
    fault = None
    if isinstance(task, dict):
        cr_error = str(task.get("connectionRequestError") or "")
        fault = task.get("fault")

    if cr_error:
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_CR_FAILED,
            f"setParameterValues queued but Connection Request failed: "
            f"{cr_error}. Force OLT `ont reset` to drain.",
        )

    if isinstance(fault, dict):
        code = fault.get("code")
        message = fault.get("message") or fault.get("detail") or "CWMP fault"
        raise ApplyError(
            action,
            ReconcileFailureReason.ACS_WRITE_FAULTED,
            f"CWMP fault {code}: {message}",
        )


def _ok(
    action: Action,
    field: str,
    old_value: Any,
    new_value: Any,
    started_monotonic: float,
) -> AppliedAction:
    return AppliedAction(
        field=field,
        surface=action.surface,
        old_value=old_value,
        new_value=new_value,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
    )


__all__ = (
    "ApplyContext",
    "ApplyError",
    "ApplyResult",
    "SecretResolver",
    "apply_plan",
    "passthrough_secret",
)
