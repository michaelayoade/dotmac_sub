"""Service helpers for remote ONT action web routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    OLTDevice,
    OntAssignment,
    OntProvisioningStatus,
    OntUnit,
    OnuMode,
    Vlan,
    WanMode,
)
from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.audit_helpers import log_audit_event
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.network.cpe import ensure_cpe_for_ont
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import run_tracked_action

logger = logging.getLogger(__name__)


def _current_user(request: Request | None) -> dict[str, Any] | None:
    if request is None:
        return None
    from app.web.admin import get_current_user

    return get_current_user(request)


def actor_name_from_request(request: Request | None) -> str:
    current_user = _current_user(request)
    return str(current_user.get("name", "unknown")) if current_user else "system"


def _actor_id_from_request(request: Request | None) -> str | None:
    current_user = _current_user(request)
    if not current_user:
        return None
    value = current_user.get("actor_id") or current_user.get("subscriber_id")
    return str(value) if value else None


def _log_action_audit(
    db: Session,
    *,
    request: Request | None,
    action: str,
    ont_id: object,
    metadata: dict[str, object] | None = None,
    status_code: int | None = None,
    is_success: bool = True,
) -> None:
    if request is None:
        return
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="ont",
        entity_id=str(ont_id),
        actor_id=_actor_id_from_request(request),
        metadata=metadata,
        status_code=status_code or 200,
        is_success=is_success,
    )


def _persist_ont_plan_step(
    db: Session,
    ont_id: str,
    step_name: str,
    values: dict[str, object],
) -> None:
    """Persist desired ONT intent even when the immediate apply path is unavailable."""
    if not any(value not in (None, "", []) for value in values.values()):
        return
    try:
        from app.services import (
            web_network_onts_provisioning as provisioning_web_service,
        )

        provisioning_web_service.update_service_order_execution_context_for_ont(
            db,
            ont_id=ont_id,
            step_name=step_name,
            values=values,
        )
    except Exception:
        logger.exception("Failed to persist %s intent for ONT %s", step_name, ont_id)


def _is_input_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(
        phrase in text
        for phrase in [
            "required",
            "invalid",
            "must be",
            "out of range",
            "at least one",
            "no wan parameters",
        ]
    )


def _intent_saved_result(result: ActionResult) -> ActionResult:
    if result.success or _is_input_error(result.message):
        return result
    return ActionResult(
        success=True,
        message=f"Intent saved. Immediate apply did not complete: {result.message}",
        data=getattr(result, "data", None),
        waiting=getattr(result, "waiting", False),
    )


def _persist_wan_intent(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    wan_vlan: int | None,
    ip_address: str | None,
    subnet_mask: str | None,
    gateway: str | None,
    dns_servers: str | None,
    instance_index: int,
) -> None:
    mode = (wan_mode or "").strip().lower()
    step_values: dict[str, object] = {
        "wan_mode": mode,
        "wan_vlan": wan_vlan,
        "ip_address": ip_address,
        "subnet_mask": subnet_mask,
        "gateway": gateway,
        "dns_servers": dns_servers,
        "instance_index": instance_index,
    }
    try:
        ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
        if mode == "bridge":
            ont.onu_mode = OnuMode.bridging
            ont.wan_mode = WanMode.setup_via_onu
        elif mode in {"dhcp", "pppoe", "static"}:
            ont.onu_mode = OnuMode.routing
            ont.wan_mode = WanMode.static_ip if mode == "static" else WanMode(mode)
        if wan_vlan is not None:
            vlan = db.scalars(select(Vlan).where(Vlan.tag == wan_vlan).limit(1)).first()
            if vlan:
                ont.wan_vlan_id = vlan.id
        db.add(ont)
        db.flush()
    except Exception:
        logger.exception("Failed to persist WAN model intent for ONT %s", ont_id)
    _persist_ont_plan_step(db, ont_id, "configure_wan_tr069", step_values)


def _normalize_fsp(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.lower().startswith("pon-"):
        raw = raw[4:].strip()
    return raw or None


def _parse_ont_id_on_olt(external_id: str | None) -> int | None:
    ext = (external_id or "").strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    if ":" in ext:
        suffix = ext.rsplit(":", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
    return None


def _display_olt_value(value: object | None) -> object | str:
    text = str(value or "").strip()
    return "—" if not text or text.lower() == "unknown" else value


def _resolve_return_olt_context(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, OLTDevice | None, str | None, int | None]:
    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)

    olt = db.get(OLTDevice, str(ont.olt_device_id)) if ont.olt_device_id else None
    board = (ont.board or "").strip()
    port = (ont.port or "").strip()
    fsp = _normalize_fsp(f"{board}/{port}") if board and port else None
    ont_id_on_olt = _parse_ont_id_on_olt(ont.external_id)
    return ont, olt, fsp, ont_id_on_olt


def execute_reboot(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Execute reboot action with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_reboot,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.reboot(db, ont_id),
        correlation_key=f"ont_reboot:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for reboot operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(
                db=db, entity_id=ont_id
            )
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id)
                    if ont and ont.olt_device_id
                    else None,
                    "method": "tr069",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    _log_action_audit(
        db,
        request=request,
        action="reboot",
        ont_id=ont_id,
        metadata={"success": result.success, "message": result.message},
    )
    return result


def execute_refresh(
    db: Session, ont_id: str, *, request: Request | None = None
) -> ActionResult:
    """Execute status refresh and return result."""
    result = OntActions.refresh_status(db, ont_id)
    _log_action_audit(
        db,
        request=request,
        action="refresh",
        ont_id=ont_id,
        metadata={"success": result.success},
    )
    return result


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def running_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for an ONT ACS running-config read."""
    result = fetch_running_config(db, ont_id)
    labels = {
        "device_info": "Device Info",
        "wan": "WAN / IP",
        "optical": "Optical",
        "wifi": "WiFi",
    }
    sections: list[dict[str, object]] = []
    for key, label in labels.items():
        values = (result.data or {}).get(key) if result.success else None
        if not isinstance(values, dict):
            continue
        rows = [
            {"key": row_key, "value": row_value}
            for row_key, row_value in values.items()
            if row_value is not None and str(row_value).strip() != ""
        ]
        if rows:
            sections.append({"key": key, "label": label, "rows": rows})
    return {
        "ont_id": ont_id,
        "config_result": result,
        "config_sections": sections,
    }


def execute_factory_reset(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Execute factory reset with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_factory_reset,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.factory_reset(db, ont_id),
        correlation_key=f"ont_factory_reset:{ont_id}",
        initiated_by=initiated_by,
    )

    # Emit audit event for factory reset operation
    if result.success:
        try:
            ont = network_service.ont_units.get_including_inactive(
                db=db, entity_id=ont_id
            )
            emit_event(
                db,
                EventType.ont_factory_reset,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(ont.olt_device_id)
                    if ont and ont.olt_device_id
                    else None,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_factory_reset event: %s", e)

    _log_action_audit(
        db,
        request=request,
        action="factory_reset",
        ont_id=ont_id,
        metadata={"success": result.success, "message": result.message},
    )
    return result


def set_wifi_ssid(
    db: Session, ont_id: str, ssid: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi SSID and return result."""
    result = OntActions.set_wifi_ssid(db, ont_id, ssid)
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_ssid",
        ont_id=ont_id,
        metadata={"success": result.success, "ssid": ssid},
    )
    return result


def set_wifi_password(
    db: Session, ont_id: str, password: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi password and return result."""
    result = OntActions.set_wifi_password(db, ont_id, password)
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_password",
        ont_id=ont_id,
        metadata={"success": result.success},
    )
    return result


def set_wifi_config(
    db: Session,
    ont_id: str,
    *,
    enabled: bool | None = None,
    ssid: str | None = None,
    password: str | None = None,
    channel: int | None = None,
    security_mode: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Set WiFi radio, SSID, security, and password fields."""
    result = OntActions.set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=ssid,
        password=password,
        channel=channel,
        security_mode=security_mode,
    )
    if result.success or not _is_input_error(result.message):
        _persist_ont_plan_step(
            db,
            ont_id,
            "configure_wifi_tr069",
            {
                "enabled": enabled,
                "ssid": ssid,
                "password_set": bool(password),
                "channel": channel,
                "security_mode": security_mode,
            },
        )
        result = _intent_saved_result(result)
    _log_action_audit(
        db,
        request=request,
        action="set_wifi_config",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "enabled": enabled,
            "ssid": ssid,
            "channel": channel,
            "security_mode": security_mode,
        },
    )
    return result


def toggle_lan_port(
    db: Session,
    ont_id: str,
    port: int,
    enabled: bool,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Toggle a LAN port and return result."""
    result = OntActions.toggle_lan_port(db, ont_id, port, enabled)
    _log_action_audit(
        db,
        request=request,
        action="toggle_lan_port",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "port": port,
            "enabled": enabled,
        },
    )
    return result


def set_lan_config(
    db: Session,
    ont_id: str,
    *,
    lan_ip: str | None = None,
    lan_subnet: str | None = None,
    dhcp_enabled: bool | None = None,
    dhcp_start: str | None = None,
    dhcp_end: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Set LAN gateway and DHCP server config on ONT via GenieACS TR-069."""
    result = OntActions.set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
    )
    if result.success or not _is_input_error(result.message):
        _persist_ont_plan_step(
            db,
            ont_id,
            "configure_lan_tr069",
            {
                "lan_ip": lan_ip,
                "lan_subnet": lan_subnet,
                "dhcp_enabled": dhcp_enabled,
                "dhcp_start": dhcp_start,
                "dhcp_end": dhcp_end,
            },
        )
        result = _intent_saved_result(result)
    _log_action_audit(
        db,
        request=request,
        action="set_lan_config",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "lan_ip": lan_ip,
            "lan_subnet": lan_subnet,
            "dhcp_enabled": dhcp_enabled,
            "dhcp_start": dhcp_start,
            "dhcp_end": dhcp_end,
        },
    )
    return result


def configure_wan_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    wan_vlan: int | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: str | None = None,
    instance_index: int = 1,
    request: Request | None = None,
) -> ActionResult:
    """Set WAN mode, VLAN, and static IP fields via GenieACS TR-069."""
    result = OntActions.configure_wan_config(
        db,
        ont_id,
        wan_mode=wan_mode,
        wan_vlan=wan_vlan,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
    )
    if result.success or not _is_input_error(result.message):
        _persist_wan_intent(
            db,
            ont_id,
            wan_mode=wan_mode,
            wan_vlan=wan_vlan,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            dns_servers=dns_servers,
            instance_index=instance_index,
        )
        result = _intent_saved_result(result)
    _log_action_audit(
        db,
        request=request,
        action="configure_wan_config",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "wan_mode": wan_mode,
            "wan_vlan": wan_vlan,
            "instance_index": instance_index,
        },
    )
    return result


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    username: str,
    password: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069 with operation tracking."""
    initiated_by = initiated_by or actor_name_from_request(request)
    result = run_tracked_action(
        db,
        NetworkOperationType.ont_set_pppoe,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: OntActions.set_pppoe_credentials(db, ont_id, username, password),
        correlation_key=f"ont_set_pppoe:{ont_id}",
        initiated_by=initiated_by,
    )
    if result.success or not _is_input_error(result.message):
        try:
            ont = network_service.ont_units.get_including_inactive(
                db=db, entity_id=ont_id
            )
            ont.pppoe_username = username.strip() or ont.pppoe_username
            db.add(ont)
            db.flush()
        except Exception:
            logger.exception("Failed to persist PPPoE username for ONT %s", ont_id)
        _persist_ont_plan_step(
            db,
            ont_id,
            "push_pppoe_tr069",
            {"username": username, "password_set": bool(password)},
        )
        result = _intent_saved_result(result)
    waiting = getattr(result, "waiting", False)
    _log_action_audit(
        db,
        request=request,
        action="set_pppoe_credentials",
        ont_id=ont_id,
        metadata={
            "result": "success"
            if result.success
            else ("waiting" if waiting else "error"),
            "message": result.message,
            "username": username,
        },
        status_code=200 if result.success else (202 if waiting else 500),
        is_success=result.success or waiting,
    )
    return result


def run_ping_diagnostic(
    db: Session,
    ont_id: str,
    host: str,
    count: int = 4,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    result = OntActions.run_ping_diagnostic(db, ont_id, host, count)
    _log_action_audit(
        db,
        request=request,
        action="ping_diagnostic",
        ont_id=ont_id,
        metadata={
            "result": "success" if result.success else "error",
            "host": host,
            "count": count,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def run_traceroute_diagnostic(
    db: Session, ont_id: str, host: str, *, request: Request | None = None
) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    result = OntActions.run_traceroute_diagnostic(db, ont_id, host)
    _log_action_audit(
        db,
        request=request,
        action="traceroute_diagnostic",
        ont_id=ont_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def execute_enable_ipv6(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Enable IPv6 dual-stack on ONT with operation tracking."""
    from app.services.network.ont_action_network import enable_ipv6_on_wan

    initiated_by = initiated_by or actor_name_from_request(request)
    return run_tracked_action(
        db,
        NetworkOperationType.ont_enable_ipv6,
        NetworkOperationTargetType.ont,
        ont_id,
        lambda: enable_ipv6_on_wan(db, ont_id),
        correlation_key=f"ont_enable_ipv6:{ont_id}",
        initiated_by=initiated_by,
    )


def execute_omci_reboot(
    db: Session, ont_id: str, *, initiated_by: str | None = None
) -> tuple[bool, str]:
    """Reboot ONT via OMCI through the OLT."""
    from app.services.network.olt_ssh_ont import reboot_ont_omci
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"

    ok, msg = reboot_ont_omci(olt, fsp, olt_ont_id)

    # Emit audit event for reboot operation
    if ok:
        try:
            emit_event(
                db,
                EventType.ont_rebooted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "fsp": fsp,
                    "ont_id_on_olt": olt_ont_id,
                    "method": "omci",
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_rebooted event: %s", e)

    return ok, msg


def configure_management_ip(
    db: Session,
    ont_id: str,
    vlan_id: int,
    ip_mode: str = "dhcp",
    priority: int | None = None,
    ip_address: str | None = None,
    subnet: str | None = None,
    gateway: str | None = None,
) -> tuple[bool, str]:
    """Configure ONT management IP via OLT IPHOST command."""
    from app.services.network.olt_ssh_ont import configure_ont_iphost
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    return configure_ont_iphost(
        olt,
        fsp,
        olt_ont_id,
        vlan_id=vlan_id,
        ip_mode=ip_mode,
        priority=priority,
        ip_address=ip_address,
        subnet=subnet,
        gateway=gateway,
    )


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Fetch ONT IPHOST config from OLT."""
    from app.services.network.olt_ssh_ont import get_ont_iphost_config
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT", {}
    return get_ont_iphost_config(olt, fsp, olt_ont_id)


def bind_tr069_profile(db: Session, ont_id: str, profile_id: int) -> tuple[bool, str]:
    """Bind TR-069 server profile to ONT via OLT."""
    from app.services.network.olt_ssh_ont import bind_tr069_server_profile
    from app.services.network.ont_provision_steps import queue_wait_tr069_bootstrap
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    ok, message = bind_tr069_server_profile(olt, fsp, olt_ont_id, profile_id)
    if ok:
        try:
            wait_result = queue_wait_tr069_bootstrap(db, ont_id)
            message = f"{message}; {wait_result.message}"
        except Exception as exc:
            logger.warning(
                "Failed to queue TR-069 bootstrap wait after manual bind for ONT %s: %s",
                ont_id,
                exc,
            )
            message = f"{message}; failed to queue ACS inform wait: {exc}"
    return ok, message


def iphost_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build management IP config context for the ONT detail partial."""
    from app.services import web_network_onts as web_network_onts_service
    from app.services.network import ont_web_forms as ont_web_forms_service

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    ok, msg, config = fetch_iphost_config(db, ont_id)
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )
    return {
        "ont": ont,
        "iphost_config": config,
        "iphost_ok": ok,
        "iphost_msg": msg,
        "initial_iphost_form": ont_web_forms_service.initial_iphost_form(ont, config),
        "vlans": vlans,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
    }


def unified_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the unified ONT configuration partial."""
    from app.services import web_network_onts as web_network_onts_service
    from app.services import web_network_service_ports as web_service_ports_service
    from app.services.network import ont_web_forms as ont_web_forms_service
    from app.services.network.ont_service_intent import (
        build_service_intent,
        load_latest_ont_plan,
    )

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    linked_tr069 = (
        db.execute(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.ont_unit_id == ont.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(Tr069CpeDevice.updated_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    ok, msg, iphost_config = fetch_iphost_config(db, ont_id)
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )
    initial_form = ont_web_forms_service.initial_iphost_form(ont, iphost_config)
    service_ports_count = 0
    try:
        service_ports_data = web_service_ports_service.list_context(db, ont_id)
        service_ports_count = len(service_ports_data.get("service_ports", []))
    except Exception:
        logger.exception("Failed to load service-port count for ONT %s", ont_id)

    assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()
    subscription = None
    subscriber_info: dict[str, object] = {}
    if assignment and assignment.subscriber_id:
        subscription = db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == assignment.subscriber_id)
            .where(Subscription.status == SubscriptionStatus.active)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        ).first()
        if assignment.subscriber:
            subscriber_info["name"] = str(
                getattr(assignment.subscriber, "display_name", "")
                or getattr(assignment.subscriber, "full_name", "")
                or ""
            ).strip()
    ont_plan = load_latest_ont_plan(
        db, subscription_id=getattr(subscription, "id", None)
    )
    service_intent = build_service_intent(
        ont,
        subscriber_info=subscriber_info,
        ont_plan=ont_plan,
    )

    snapshot = getattr(ont, "tr069_last_snapshot", None) or {}
    wireless_snapshot = snapshot.get("wireless") if isinstance(snapshot, dict) else {}
    current_ssid = None
    if isinstance(wireless_snapshot, dict):
        current_ssid = wireless_snapshot.get("SSID") or wireless_snapshot.get("ssid")

    return {
        "ont": ont,
        "service_intent": service_intent,
        "ont_plan": ont_plan,
        "iphost_config": iphost_config,
        "iphost_ok": ok,
        "iphost_msg": msg,
        "initial_iphost_form": initial_form,
        "vlans": vlans,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
        "mgmt_ip_summary": {
            "mode": initial_form.get("ip_mode"),
            "vlan": initial_form.get("vlan_id"),
            "ip": initial_form.get("ip_address")
            if initial_form.get("ip_mode") == "static"
            else None,
        },
        "service_ports_count": service_ports_count,
        "wan_summary": {
            "pppoe_user": getattr(ont, "pppoe_username", None),
            "wan_ip": getattr(ont, "observed_wan_ip", None),
            "status": getattr(ont, "observed_pppoe_status", None),
        },
        "wifi_summary": {"ssid": current_ssid},
        "has_tr069": bool(
            linked_tr069 and str(getattr(linked_tr069, "genieacs_device_id", "") or "")
        ),
    }


def wan_config_context(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_ont_tr069 as web_tr069_service
    from app.services import web_network_onts as web_network_onts_service
    from app.services.network.ont_service_intent import load_ont_plan_for_ont

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    ont_plan = load_ont_plan_for_ont(db, ont_id=ont_id)
    tr069_data = web_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = tr069_data.get("tr069")
    wan = getattr(tr069, "wan", None) if tr069 else None
    return {
        "ont_id": ont_id,
        "tr069_available": bool(getattr(tr069, "available", False)) if tr069 else False,
        "ont": ont,
        "ont_plan": ont_plan,
        "wan_info": wan,
        "current_pppoe_user": (wan or {}).get("Username"),
        "vlans": web_network_onts_service.get_vlans_for_ont(db, ont),
    }


def wifi_config_context(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_ont_tr069 as web_tr069_service
    from app.services.network.ont_service_intent import load_ont_plan_for_ont

    ont_plan = load_ont_plan_for_ont(db, ont_id=ont_id)
    tr069_data = web_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = tr069_data.get("tr069")
    wireless = getattr(tr069, "wireless", None) if tr069 else None
    return {
        "ont_id": ont_id,
        "tr069_available": bool(getattr(tr069, "available", False)) if tr069 else False,
        "ont_plan": ont_plan,
        "wireless_info": wireless,
        "current_ssid": (wireless or {}).get("SSID"),
    }


def tr069_profile_config_context(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_onts as web_network_onts_service

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    tr069_profiles, tr069_profiles_error = (
        web_network_onts_service.get_tr069_profiles_for_ont(db, ont)
    )
    return {
        "ont_id": ont_id,
        "tr069_profiles": tr069_profiles,
        "tr069_profiles_error": tr069_profiles_error,
        "current_profile": None,
        "current_profile_id": None,
    }


def lan_config_context(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_ont_tr069 as web_tr069_service
    from app.services.network.ont_service_intent import load_ont_plan_for_ont

    ont_plan = load_ont_plan_for_ont(db, ont_id=ont_id)
    tr069_data = web_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = tr069_data.get("tr069")
    return {
        "ont_id": ont_id,
        "tr069_available": bool(getattr(tr069, "available", False)) if tr069 else False,
        "ont_plan": ont_plan,
        "lan_info": getattr(tr069, "lan", None) if tr069 else None,
        "ethernet_ports": getattr(tr069, "ethernet_ports", None) if tr069 else None,
        "lan_hosts": getattr(tr069, "lan_hosts", None) if tr069 else None,
    }


def diagnostics_config_context(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_ont_tr069 as web_tr069_service

    tr069_data = web_tr069_service.tr069_tab_data(db, ont_id)
    tr069 = tr069_data.get("tr069")
    return {
        "ont_id": ont_id,
        "tr069_available": bool(getattr(tr069, "available", False)) if tr069 else False,
    }


def _intent_step_present(ont_plan: dict[str, Any], step_name: str) -> bool:
    section = ont_plan.get(step_name)
    return isinstance(section, dict) and any(
        value not in (None, "", []) for value in section.values()
    )


def _runbook_step(
    *,
    order: int,
    title: str,
    source: str,
    status: str,
    message: str,
    action_label: str | None = None,
    action_url: str | None = None,
    method: str = "GET",
    confirm: str | None = None,
    loading_label: str | None = None,
    target: str | None = None,
    swap: str = "innerHTML",
    group: str = "Provisioning",
) -> dict[str, object]:
    return {
        "order": order,
        "title": title,
        "source": source,
        "status": status,
        "message": message,
        "action_label": action_label,
        "action_url": action_url,
        "method": method,
        "confirm": confirm,
        "loading_label": loading_label,
        "target": target,
        "swap": swap,
        "group": group,
    }


def _build_ont_operations_runbook(
    *,
    ont: OntUnit | None,
    olt: OLTDevice | None,
    fsp: str | None,
    ont_id_on_olt: int | None,
    linked_tr069: Tr069CpeDevice | None,
    service_intent: dict[str, object],
    ont_plan: dict[str, Any],
    snapshots: list[object],
) -> list[dict[str, object]]:
    if not ont:
        return []

    ont_id = str(ont.id)
    has_olt_context = bool(olt and fsp and ont_id_on_olt is not None)
    has_acs_device = bool(linked_tr069 and linked_tr069.genieacs_device_id)
    has_cr_url = bool(linked_tr069 and linked_tr069.connection_request_url)
    intent_complete = bool(service_intent.get("is_complete"))
    missing_count = int(service_intent.get("missing_count") or 0)
    has_service_path_intent = _intent_step_present(ont_plan, "create_service_port")
    has_mgmt_intent = bool(
        getattr(ont, "mgmt_vlan_id", None)
        or getattr(ont, "mgmt_ip_mode", None)
        or _intent_step_present(ont_plan, "configure_management_ip")
    )
    has_wan_intent = bool(
        getattr(ont, "wan_vlan_id", None)
        or getattr(ont, "wan_mode", None)
        or _intent_step_present(ont_plan, "configure_wan_tr069")
    )
    has_pppoe_intent = bool(
        getattr(ont, "pppoe_username", None)
        or _intent_step_present(ont_plan, "push_pppoe_tr069")
        or _intent_step_present(ont_plan, "push_pppoe_omci")
    )
    has_lan_intent = _intent_step_present(ont_plan, "configure_lan_tr069")
    has_wifi_intent = _intent_step_present(ont_plan, "configure_wifi_tr069")
    has_running_snapshot = bool(snapshots)

    return [
        _runbook_step(
            order=1,
            title="OLT placement",
            source="OLT",
            status="complete" if has_olt_context else "blocked",
            message=(
                f"{getattr(olt, 'name', 'OLT')} / {fsp} / ONT {ont_id_on_olt}"
                if has_olt_context
                else "Set OLT, F/S/P, and OLT ONT-ID before access actions."
            ),
            action_label="Rediscover",
            action_url=f"/admin/network/onts/{ont_id}/reconcile",
            method="POST",
            confirm="Run OLT/ACS reconciliation for this ONT?",
            loading_label="Reconciling...",
            target="#operational-health-container",
        ),
        _runbook_step(
            order=2,
            title="Service intent",
            source="Local intent",
            status="complete" if intent_complete else "blocked",
            message=(
                "Management, internet, LAN, WiFi, and service path intent are set."
                if intent_complete
                else f"{missing_count} intent fields are unset."
            ),
            action_label="Edit intent",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=3,
            title="OLT service path",
            source="OLT SSH",
            status="ready" if has_olt_context and has_service_path_intent else "blocked",
            message=(
                "Ready to create or verify service-port/VLAN path."
                if has_olt_context and has_service_path_intent
                else "Set internet VLAN/service-port intent and confirm OLT placement."
            ),
            action_label="Service ports",
            action_url=f"/admin/network/onts/{ont_id}?tab=service-ports",
        ),
        _runbook_step(
            order=4,
            title="Management access",
            source="OLT SSH",
            status="ready" if has_olt_context and has_mgmt_intent else "blocked",
            message=(
                "Management VLAN/IP intent is ready to apply or verify."
                if has_olt_context and has_mgmt_intent
                else "Set management VLAN and IP method first."
            ),
            action_label="Management intent",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=5,
            title="ACS inform",
            source="ACS",
            status="complete"
            if has_acs_device
            else "waiting"
            if getattr(ont, "tr069_acs_server_id", None)
            or getattr(olt, "tr069_acs_server_id", None)
            else "blocked",
            message=(
                f"ACS device {linked_tr069.genieacs_device_id}"
                if has_acs_device and linked_tr069
                else "Waiting for ONT to inform ACS."
                if getattr(ont, "tr069_acs_server_id", None)
                or getattr(olt, "tr069_acs_server_id", None)
                else "Bind an ACS profile before waiting for inform."
            ),
            action_label="Wait for inform",
            action_url=f"/admin/network/onts/{ont_id}/step/wait-tr069-bootstrap",
            method="POST",
            confirm="Queue an ACS bootstrap wait for this ONT?",
            loading_label="Waiting...",
            target=None,
            swap="none",
        ),
        _runbook_step(
            order=6,
            title="ACS connection request",
            source="ACS",
            status="ready" if has_cr_url else "blocked",
            message="Connection request URL captured." if has_cr_url else "No connection request URL captured yet.",
            action_label="Force inform",
            action_url=f"/admin/network/onts/{ont_id}/connection-request",
            method="POST",
            confirm="Send a TR-069 connection request to this ONT?",
            loading_label="Sending...",
            target=None,
            swap="none",
        ),
        _runbook_step(
            order=7,
            title="WAN service",
            source="ACS",
            status="ready" if has_acs_device and has_wan_intent else "blocked",
            message=(
                "WAN method/VLAN intent is ready to apply from the config tab."
                if has_acs_device and has_wan_intent
                else "Set WAN method/VLAN intent and wait for ACS reachability."
            ),
            action_label="WAN intent",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=8,
            title="PPPoE/static credentials",
            source="ACS",
            status="ready" if has_acs_device and has_pppoe_intent else "blocked",
            message=(
                "Credentials or static addressing intent is ready to apply."
                if has_acs_device and has_pppoe_intent
                else "Set PPPoE/static addressing intent first."
            ),
            action_label="Internet credentials",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=9,
            title="LAN and DHCP",
            source="ACS",
            status="ready" if has_acs_device and has_lan_intent else "blocked",
            message=(
                "LAN gateway and DHCP intent is ready to apply."
                if has_acs_device and has_lan_intent
                else "Set LAN gateway/DHCP intent and wait for ACS reachability."
            ),
            action_label="LAN intent",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=10,
            title="WiFi",
            source="ACS",
            status="ready" if has_acs_device and has_wifi_intent else "blocked",
            message=(
                "WiFi intent is ready to apply."
                if has_acs_device and has_wifi_intent
                else "Set WiFi intent and wait for ACS reachability."
            ),
            action_label="WiFi intent",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
        _runbook_step(
            order=11,
            title="Running config snapshot",
            source="ACS read",
            status="complete" if has_running_snapshot else "ready" if has_acs_device else "blocked",
            message=(
                f"{len(snapshots)} snapshot(s) captured."
                if has_running_snapshot
                else "Capture running config after ACS is reachable."
            ),
            action_label="Capture snapshot",
            action_url=f"/admin/network/onts/{ont_id}/config-snapshot",
            method="POST",
            confirm="Capture current running config from this ONT?",
            loading_label="Capturing...",
            target="#snapshot-list",
        ),
        _runbook_step(
            order=12,
            title="Intent verification",
            source="Intent vs running",
            status="ready" if has_running_snapshot and intent_complete else "blocked",
            message=(
                "Ready to compare desired intent with captured running state."
                if has_running_snapshot and intent_complete
                else "Complete service intent and capture running config first."
            ),
            action_label="Review config",
            action_url=f"/admin/network/onts/{ont_id}?tab=configuration",
        ),
    ]


def operational_health_context(
    db: Session,
    ont_id: str,
    *,
    message: str | None = None,
    message_type: str = "info",
    limit: int = 5,
) -> dict[str, object]:
    """Build ONT operational action/readiness context for the detail page."""
    ont, olt, fsp, ont_id_on_olt = _resolve_return_olt_context(db, ont_id)
    linked_tr069 = (
        db.execute(
            select(Tr069CpeDevice)
            .where(Tr069CpeDevice.ont_unit_id == ont.id)
            .where(Tr069CpeDevice.is_active.is_(True))
            .order_by(Tr069CpeDevice.last_inform_at.desc().nullslast())
            .limit(1)
        )
        .scalars()
        .first()
        if ont
        else None
    )
    snapshots = []
    try:
        snapshots = _config_snapshot_service().list_for_ont(db, ont_id, limit=limit)
    except HTTPException:
        snapshots = []
    try:
        from app.services.network.ont_service_intent import (
            build_service_intent,
            load_ont_plan_for_ont,
        )

        ont_plan = load_ont_plan_for_ont(db, ont_id=ont_id)
        service_intent = build_service_intent(ont, ont_plan=ont_plan) if ont else {}
    except Exception:
        logger.exception("Failed to build operations runbook intent for ONT %s", ont_id)
        ont_plan = {}
        service_intent = {}
    operations_runbook = _build_ont_operations_runbook(
        ont=ont,
        olt=olt,
        fsp=fsp,
        ont_id_on_olt=ont_id_on_olt,
        linked_tr069=linked_tr069,
        service_intent=service_intent,
        ont_plan=ont_plan,
        snapshots=snapshots,
    )

    checks = [
        {
            "label": "OLT linked",
            "ok": bool(olt),
            "message": getattr(olt, "name", None) if olt else "No OLT on ONT record",
        },
        {
            "label": "F/S/P known",
            "ok": bool(fsp),
            "message": fsp or "Board/port missing",
        },
        {
            "label": "OLT ONT-ID known",
            "ok": ont_id_on_olt is not None,
            "message": str(ont_id_on_olt)
            if ont_id_on_olt is not None
            else "external_id missing",
        },
        {
            "label": "ACS linked",
            "ok": bool(linked_tr069 and linked_tr069.genieacs_device_id),
            "message": (
                str(linked_tr069.genieacs_device_id)
                if linked_tr069 and linked_tr069.genieacs_device_id
                else "Waiting for ACS inform"
            ),
        },
        {
            "label": "Connection request URL",
            "ok": bool(linked_tr069 and linked_tr069.connection_request_url),
            "message": "Ready"
            if linked_tr069 and linked_tr069.connection_request_url
            else "Not captured",
        },
        {
            "label": "PPPoE stored",
            "ok": bool(getattr(ont, "pppoe_username", None)),
            "message": getattr(ont, "pppoe_username", None) or "No PPPoE username",
        },
    ]
    return {
        "ont": ont,
        "ont_id": ont_id,
        "olt": olt,
        "fsp": fsp,
        "ont_id_on_olt": ont_id_on_olt,
        "linked_tr069": linked_tr069,
        "operational_checks": checks,
        "operation_message": message,
        "operation_message_type": message_type,
        "operations_runbook": operations_runbook,
        "config_snapshots": snapshots,
        "return_impact": {
            "service_ports": "OLT service ports will be removed when reachable.",
            "olt_registration": "ONT authorization will be removed from the OLT.",
            "assignment": "Active assignment will be closed.",
            "credentials": "Local PPPoE and management config will be cleared.",
            "acs": "ACS link remains discoverable by serial after the next inform.",
        },
    }


def reconcile_operational_state(
    db: Session,
    ont_id: str,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Run the safest available ONT rediscovery/reconciliation path."""
    ont, olt, fsp, ont_id_on_olt = _resolve_return_olt_context(db, ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")
    if not olt:
        return ActionResult(success=False, message="ONT has no associated OLT")

    messages: list[str] = []
    success = False
    if fsp and ont_id_on_olt is not None:
        from app.services.network.olt_snmp_sync import sync_authorized_ont_from_olt_snmp

        ok, msg, _stats = sync_authorized_ont_from_olt_snmp(
            db,
            olt_id=str(olt.id),
            ont_unit_id=str(ont.id),
            fsp=fsp,
            ont_id_on_olt=ont_id_on_olt,
            serial_number=ont.serial_number,
        )
        messages.append(msg)
        success = ok
    else:
        messages.append("Skipped targeted SNMP sync: F/S/P or OLT ONT-ID missing.")

    try:
        from app.services import web_network_ont_autofind as autofind_service

        ok, msg, stats = autofind_service.sync_olt_autofind_candidates(db, str(olt.id))
        discovered = stats.get("discovered", 0) if stats else 0
        messages.append(f"{msg} ({discovered} autofind entries)")
        success = success or ok
    except Exception as exc:
        logger.exception("Failed to refresh autofind during ONT reconcile %s", ont_id)
        messages.append(f"Autofind refresh failed: {exc}")

    if getattr(ont, "tr069_acs_server_id", None) or getattr(
        olt, "tr069_acs_server_id", None
    ):
        try:
            from app.services.network.ont_provision_steps import (
                queue_wait_tr069_bootstrap,
            )

            wait_result = queue_wait_tr069_bootstrap(db, ont_id)
            messages.append(wait_result.message)
            success = success or wait_result.success
        except Exception as exc:
            logger.exception("Failed to queue TR-069 bootstrap wait for ONT %s", ont_id)
            messages.append(f"ACS inform wait failed: {exc}")

    _log_action_audit(
        db,
        request=request,
        action="reconcile_operational_state",
        ont_id=ont_id,
        metadata={"messages": messages},
        is_success=success,
    )
    return ActionResult(success=success, message="; ".join(messages))


def return_to_inventory_for_web(
    db: Session,
    ont_id: str,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Return ONT to inventory with route-friendly not-found handling."""
    try:
        network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    except HTTPException:
        return ActionResult(success=False, message="ONT not found")
    return return_to_inventory(db, ont_id, request=request)


def _config_snapshot_service():
    try:
        from app.services.network.ont_config_snapshots import ont_config_snapshots
    except ImportError as exc:
        raise HTTPException(
            status_code=501,
            detail="Config snapshots not available",
        ) from exc
    return ont_config_snapshots


def capture_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    label: str | None,
    limit: int = 5,
) -> tuple[dict[str, object], str | None]:
    """Capture a config snapshot and return refreshed list context plus error."""
    snapshots_service = _config_snapshot_service()
    error_msg: str | None = None
    try:
        snapshots_service.capture(db, ont_id, label=label)
    except HTTPException as exc:
        error_msg = str(exc.detail)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }, error_msg


def config_snapshot_detail_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
) -> dict[str, object]:
    """Return context for a single ONT config snapshot detail."""
    snapshot = _config_snapshot_service().get(db, snapshot_id, ont_id=ont_id)
    return {"snapshot": snapshot}


def delete_config_snapshot_list_context(
    db: Session,
    *,
    ont_id: str,
    snapshot_id: str,
    limit: int = 5,
) -> dict[str, object]:
    """Delete a config snapshot and return refreshed list context."""
    snapshots_service = _config_snapshot_service()
    snapshots_service.delete(db, snapshot_id, ont_id=ont_id)
    return {
        "ont_id": ont_id,
        "config_snapshots": snapshots_service.list_for_ont(db, ont_id, limit=limit),
    }


def _cleanup_olt_state_for_return(
    db: Session, ont_id: str
) -> tuple[bool, list[str], list[str]]:
    """Remove service ports and deauthorize ONT from OLT.

    Returns:
        (success, completed_steps, errors)
    """
    from app.services.network.olt_ssh_ont import deauthorize_ont
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    completed: list[str] = []
    errors: list[str] = []

    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return False, completed, ["ONT not found"]
    if not olt or not fsp or olt_ont_id is None:
        # No OLT context to clean up - that's OK
        return True, completed, errors

    ok, msg, service_ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        errors.append(f"Cannot read OLT service-ports: {msg}")
        return False, completed, errors

    for service_port in service_ports:
        ok, msg = delete_service_port(olt, service_port.index)
        if not ok:
            errors.append(f"Failed to remove service-port {service_port.index}: {msg}")
            return False, completed, errors
        completed.append(f"Removed service-port {service_port.index}")

    ok, msg = deauthorize_ont(olt, fsp, olt_ont_id)
    if not ok:
        errors.append(f"Failed to deauthorize ONT: {msg}")
        return False, completed, errors
    completed.append("Deauthorized ONT from OLT")

    return True, completed, errors


def return_to_inventory(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Release an ONT from the OLT, close assignment, and clear service state."""
    from app.services.network.olt_ssh_ont import deauthorize_ont
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port,
        get_service_ports_for_ont,
    )

    initiated_by = initiated_by or actor_name_from_request(request)
    ont, olt, fsp, olt_ont_id = _resolve_return_olt_context(db, ont_id)
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")
    if not olt or not fsp or olt_ont_id is None:
        return ActionResult(
            success=False,
            message="Cannot resolve OLT context for this ONT.",
        )

    ok, msg, service_ports = get_service_ports_for_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Cannot read OLT service-ports before release: {msg}",
        )

    deleted_service_ports = 0
    for service_port in service_ports:
        ok, msg = delete_service_port(olt, service_port.index)
        if not ok:
            return ActionResult(
                success=False,
                message=(
                    f"Failed to remove OLT service-port {service_port.index}: {msg}"
                ),
            )
        deleted_service_ports += 1

        # Emit audit event for service port deletion
        try:
            emit_event(
                db,
                EventType.ont_service_port_deleted,
                {
                    "ont_id": ont_id,
                    "ont_serial": ont.serial_number if ont else None,
                    "olt_id": str(olt.id),
                    "olt_name": olt.name,
                    "service_port_index": service_port.index,
                },
                actor=initiated_by or "system",
            )
        except Exception as e:
            logger.warning("Failed to emit ont_service_port_deleted event: %s", e)

    ok, msg = deauthorize_ont(olt, fsp, olt_ont_id)
    if not ok:
        return ActionResult(
            success=False,
            message=f"Failed to delete ONT from OLT: {msg}",
        )

    # Emit audit event for ONT deauthorization
    try:
        emit_event(
            db,
            EventType.ont_deauthorized,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "olt_id": str(olt.id),
                "olt_name": olt.name,
                "fsp": fsp,
                "ont_id_on_olt": olt_ont_id,
            },
            actor=initiated_by or "system",
        )
    except Exception as e:
        logger.warning("Failed to emit ont_deauthorized event: %s", e)

    active_assignment = db.scalars(
        select(OntAssignment)
        .where(
            OntAssignment.ont_unit_id == ont.id,
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
        .limit(1)
    ).first()

    if active_assignment is not None:
        active_assignment.active = False

    ont.is_active = False
    ont.provisioning_profile_id = None
    ont.provisioning_status = OntProvisioningStatus.unprovisioned
    ont.last_provisioned_at = None
    ont.external_id = None
    ont.wan_vlan_id = None
    ont.wan_mode = None
    ont.config_method = None
    ont.ip_protocol = None
    ont.pppoe_username = None
    ont.pppoe_password = None
    ont.wan_remote_access = False
    ont.tr069_acs_server_id = None
    ont.mgmt_ip_mode = None
    ont.mgmt_vlan_id = None
    ont.mgmt_ip_address = None
    ont.mgmt_remote_access = False
    ont.voip_enabled = False
    db.flush()
    ensure_cpe_for_ont(db, ont, commit=False, strict_existing_match=False)

    db.commit()
    db.refresh(ont)

    assignment_msg = "assignment closed and " if active_assignment is not None else ""
    service_port_msg = (
        f"{deleted_service_ports} service-port(s) removed, "
        if deleted_service_ports
        else ""
    )
    result = ActionResult(
        success=True,
        message=(
            "ONT returned to inventory: "
            f"{service_port_msg}{assignment_msg}removed from OLT and service state cleared."
        ),
    )
    _log_action_audit(
        db,
        request=request,
        action="return_to_inventory",
        ont_id=ont.id,
        metadata={"serial_number": ont.serial_number},
    )
    return result


def apply_profile(
    db: Session, ont_id: str, profile_id: str, *, request: Request | None = None
) -> Any:
    """Apply a profile template and audit the explicit admin action."""
    from app.services.network.ont_profile_apply import apply_profile_to_ont

    result = apply_profile_to_ont(db, ont_id, profile_id)
    _log_action_audit(
        db,
        request=request,
        action="apply_profile",
        ont_id=ont_id,
        metadata={
            "profile_id": profile_id,
            "success": result.success,
            "fields_updated": result.fields_updated,
        },
    )
    return result


def firmware_upgrade(
    db: Session, ont_id: str, firmware_image_id: str, *, request: Request | None = None
) -> ActionResult:
    """Trigger firmware upgrade and audit the admin action."""
    result = OntActions.firmware_upgrade(db, ont_id, firmware_image_id)
    _log_action_audit(
        db,
        request=request,
        action="firmware_upgrade",
        ont_id=ont_id,
        metadata={"firmware_image_id": firmware_image_id, "success": result.success},
    )
    return result


def execute_connection_request(
    db: Session,
    ont_id: str,
    *,
    initiated_by: str | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Send a TR-069 connection request with operation tracking."""
    from app.services.network.ont_action_network import send_connection_request_tracked

    initiated_by = initiated_by or actor_name_from_request(request)
    return send_connection_request_tracked(db, ont_id, initiated_by=initiated_by)


def fetch_olt_side_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch ONT config/state from OLT side via SSH-backed services."""
    ont, olt, fsp, ont_id_on_olt = _resolve_return_olt_context(db, ont_id)
    if not ont:
        return ActionResult(success=False, message="ONT not found")
    if not olt:
        return ActionResult(success=False, message="ONT has no associated OLT")
    if not fsp or ont_id_on_olt is None:
        return ActionResult(
            success=False,
            message="ONT is missing a usable F/S/P or OLT ONT-ID.",
        )

    status_text = ""
    iphost_text = ""
    service_ports_text = ""

    try:
        from app.services.network.olt_ssh_ont import get_ont_status

        ok, msg, status = get_ont_status(olt, fsp, ont_id_on_olt)
        if ok and status:
            status_text = "\n".join(
                [
                    f"Serial Number: {_display_olt_value(status.serial_number)}",
                    f"F/S/P: {fsp}",
                    f"ONT-ID: {ont_id_on_olt}",
                    f"Run State: {_display_olt_value(status.run_state)}",
                    f"Config State: {_display_olt_value(status.config_state)}",
                    f"Match State: {_display_olt_value(status.match_state)}",
                ]
            )
        else:
            status_text = msg
    except Exception as exc:
        logger.exception("Failed to read OLT ONT status for ONT %s", ont_id)
        status_text = f"Status read failed: {exc}"

    ok, msg, iphost = fetch_iphost_config(db, ont_id)
    if ok and iphost:
        iphost_text = "\n".join(f"{key}: {value}" for key, value in iphost.items())
    else:
        iphost_text = msg

    try:
        from app.services import web_network_service_ports as service_ports_service

        ports_data = service_ports_service.list_context(db, ont_id)
        ports = ports_data.get("service_ports") or []
        if ports:
            lines = []
            for port in ports:
                if isinstance(port, dict):
                    lines.append(
                        " ".join(
                            str(part)
                            for part in [
                                f"index={port.get('index', '-')}",
                                f"vlan={port.get('vlan', port.get('vlan_id', '-'))}",
                                f"gem={port.get('gem', port.get('gem_index', '-'))}",
                                f"state={port.get('state', port.get('status', '-'))}",
                            ]
                        )
                    )
                else:
                    lines.append(str(port))
            service_ports_text = "\n".join(lines)
        else:
            service_ports_text = "No service ports returned for this ONT."
    except Exception as exc:
        logger.exception("Failed to read OLT service ports for ONT %s", ont_id)
        service_ports_text = f"Service-port read failed: {exc}"

    return ActionResult(
        success=True,
        message="OLT-side config retrieved.",
        data={
            "ont_info": status_text,
            "ont_wan": iphost_text,
            "service_ports": service_ports_text,
        },
    )


def olt_side_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT config."""
    result = fetch_olt_side_config(db, ont_id)
    section_labels = {
        "ont_info": "ONT Info",
        "ont_wan": "WAN Info",
        "service_ports": "Service Ports",
    }
    sections = []
    for key, label in section_labels.items():
        content = (result.data or {}).get(key) if result.success else None
        if content:
            sections.append({"key": key, "label": label, "content": content})
    return {"result": result, "sections": sections}


def fetch_olt_status(db: Session, ont_id: str) -> dict[str, Any]:
    """Query the OLT directly for ONT registration state (GPON layer).

    Returns a dict with success, message, and optional entry data.
    """
    ont, olt, fsp, ont_id_on_olt = _resolve_return_olt_context(db, ont_id)
    if not ont:
        return {"success": False, "message": "ONT not found"}
    if not olt:
        return {"success": False, "message": "ONT has no associated OLT"}
    if not fsp or ont_id_on_olt is None:
        return {
            "success": False,
            "message": "ONT is missing a usable F/S/P or OLT ONT-ID.",
        }

    from app.services.network.olt_ssh_ont import get_ont_status

    ok, msg, status = get_ont_status(olt, fsp, ont_id_on_olt)
    if not ok or status is None:
        return {"success": False, "message": msg}

    return {
        "success": True,
        "message": msg,
        "entry": {
            "run_state": status.run_state,
            "config_state": status.config_state,
            "match_state": status.match_state,
            "serial_number": status.serial_number,
            "fsp": fsp,
            "ont_id": ont_id_on_olt,
            "onu_rx_signal_dbm": getattr(ont, "onu_rx_signal_dbm", None),
            "olt_rx_signal_dbm": getattr(ont, "olt_rx_signal_dbm", None),
        },
    }


def olt_status_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT status."""
    result = fetch_olt_status(db, ont_id)
    entry = result.get("entry") or {}
    raw_run_state = str(
        entry.get("run_state") or entry.get("online_status") or ""
    ).lower()
    run_state = "" if raw_run_state == "unknown" else raw_run_state
    rows = [
        (
            "Run State",
            _display_olt_value(entry.get("run_state") or entry.get("online_status")),
        ),
        ("Config State", _display_olt_value(entry.get("config_state"))),
        ("Match State", _display_olt_value(entry.get("match_state"))),
        ("Serial", _display_olt_value(entry.get("serial_number"))),
        ("F/S/P", entry.get("fsp") or "—"),
        ("ONT-ID", entry.get("ont_id") or "—"),
        ("Last Down Cause", entry.get("last_down_cause") or "—"),
        ("Last Down Time", entry.get("last_down_time") or "—"),
        ("Last Up Time", entry.get("last_up_time") or "—"),
        ("Description", entry.get("description") or "—"),
    ]
    return {
        "result": result,
        "entry": entry,
        "run_state": run_state,
        "rows": rows,
    }


def resolve_stored_pppoe_password(db: Session, ont_id: str) -> str:
    """Decrypt and return the stored PPPoE password for an ONT."""
    from app.models.network import OntUnit
    from app.services.credential_crypto import decrypt_credential

    ont = db.get(OntUnit, ont_id)
    if not ont:
        return ""

    raw = getattr(ont, "pppoe_password", None)
    if not raw:
        return ""

    try:
        return decrypt_credential(raw) or ""
    except Exception:
        logger.warning("Failed to decrypt PPPoE password for ONT %s", ont_id)
        return ""


def reveal_stored_pppoe_password(
    db: Session, ont_id: str, *, request: Request | None = None
) -> tuple[str, bool]:
    """Return stored PPPoE password and audit the reveal action."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return "", False

    password = resolve_stored_pppoe_password(db, ont_id)
    _log_action_audit(
        db,
        request=request,
        action="reveal_pppoe_password",
        ont_id=ont_id,
        metadata={"username": ont.pppoe_username or ""},
    )
    return password, True
