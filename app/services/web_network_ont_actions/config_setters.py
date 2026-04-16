"""Configuration setters for ONT web actions."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network_operation import (
    NetworkOperationTargetType,
    NetworkOperationType,
)
from app.services import network as network_service
from app.services.network.ont_actions import ActionResult, OntActions
from app.services.network_operations import run_tracked_action
from app.services.web_network_ont_actions._common import (
    _intent_saved_result,
    _is_input_error,
    _log_action_audit,
    _persist_ont_plan_step,
    _persist_wan_intent,
    actor_name_from_request,
)

logger = logging.getLogger(__name__)


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
            ont.tr069_olt_profile_id = profile_id
            db.add(ont)
            db.flush()
            _persist_ont_plan_step(
                db,
                ont_id,
                "bind_tr069",
                {"tr069_olt_profile_id": profile_id},
            )
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
