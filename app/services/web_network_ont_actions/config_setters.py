"""Configuration setters for ONT web actions."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OntUnit
from app.services.acs_client import create_acs_config_writer
from app.services.credential_crypto import encrypt_credential
from app.services.network.olt_config_pack import resolve_olt_config_pack
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_desired_config import (
    upsert_ont_desired_config_value,
)
from app.services.web_network_ont_actions._common import (
    _intent_saved_result,
    _log_action_audit,
    _persist_ont_plan_step,
    actor_name_from_request,
)

logger = logging.getLogger(__name__)


def _acs_config_writer():
    return create_acs_config_writer()


def set_wifi_ssid(
    db: Session, ont_id: str, ssid: str, *, request: Request | None = None
) -> ActionResult:
    """Set WiFi SSID and return result."""
    result = _acs_config_writer().set_wifi_ssid(db, ont_id, ssid)
    if result.success:
        ont = db.get(OntUnit, ont_id)
        if ont:
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wifi.ssid",
                value=ssid,
                reason="config_setters.set_wifi_ssid",
            )
            db.flush()
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
    result = _acs_config_writer().set_wifi_password(db, ont_id, password)
    if result.success:
        ont = db.get(OntUnit, ont_id)
        if ont:
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wifi.password",
                value=encrypt_credential(password),
                reason="config_setters.set_wifi_password",
            )
            db.flush()
        # Emit audit event for credential change
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_wifi_password_set,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "password_set": True,
                "method": "tr069",
                "result": "success",
            },
            actor=actor_name_from_request(request),
        )
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
    result = _acs_config_writer().set_wifi_config(
        db,
        ont_id,
        enabled=enabled,
        ssid=ssid,
        password=password,
        channel=channel,
        security_mode=security_mode,
    )
    if result.success:
        ont = db.get(OntUnit, ont_id)
        if ont:
            # Store WiFi config as overrides
            if ssid is not None:
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name="wifi.ssid",
                    value=ssid,
                    reason="config_setters.set_wifi_config",
                )
            if password is not None:
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name="wifi.password",
                    value=encrypt_credential(password),
                    reason="config_setters.set_wifi_config",
                )
            if enabled is not None:
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name="wifi.enabled",
                    value=enabled,
                    reason="config_setters.set_wifi_config",
                )
            if channel is not None:
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name="wifi.channel",
                    value=channel,
                    reason="config_setters.set_wifi_config",
                )
            if security_mode is not None:
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name="wifi.security_mode",
                    value=security_mode,
                    reason="config_setters.set_wifi_config",
                )
            db.flush()
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
        # Emit audit event for WiFi config change
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_wifi_config_updated,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "enabled": enabled,
                "ssid_updated": ssid is not None,
                "password_set": password is not None,
                "channel": channel,
                "security_mode": security_mode,
                "method": "tr069",
                "result": "success",
            },
            actor=actor_name_from_request(request),
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
    result = _acs_config_writer().toggle_lan_port(db, ont_id, port, enabled)
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
    """Set LAN gateway and DHCP server config on ONT via GenieACS TR-069.

    LAN settings are explicit ONT-local desired config because they are
    customer-specific runtime intent, not OLT config-pack defaults.
    """
    result = _acs_config_writer().set_lan_config(
        db,
        ont_id,
        lan_ip=lan_ip,
        lan_subnet=lan_subnet,
        dhcp_enabled=dhcp_enabled,
        dhcp_start=dhcp_start,
        dhcp_end=dhcp_end,
    )
    if result.success:
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
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if ont is None:
        return False, "ONT not found"
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    result = get_protocol_adapter(olt).configure_iphost(
        fsp,
        olt_ont_id,
        vlan=vlan_id,
        mode=ip_mode,
        priority=priority,
        ip_address=ip_address,
        subnet_mask=subnet,
        gateway=gateway,
    )
    return result.success, result.message


def bind_tr069_profile(db: Session, ont_id: str, profile_id: int) -> tuple[bool, str]:
    """Bind TR-069 server profile to ONT via OLT."""
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.ont_provision_steps import wait_tr069_bootstrap
    from app.services.web_network_service_ports import _resolve_ont_olt_context

    ont, olt, fsp, olt_ont_id = _resolve_ont_olt_context(db, ont_id)
    if ont is None:
        return False, "ONT not found"
    if not olt or not fsp or olt_ont_id is None:
        return False, "Cannot resolve OLT context for this ONT"
    bind_result = get_protocol_adapter(olt).bind_tr069_profile(
        fsp,
        olt_ont_id,
        profile_id=profile_id,
    )
    ok = bind_result.success
    message = bind_result.message
    if ok:
        try:
            _persist_ont_plan_step(
                db,
                ont_id,
                "bind_tr069",
                {"tr069_olt_profile_id": profile_id},
            )
            wait_result = wait_tr069_bootstrap(db, ont_id)
            message = f"{message}; {wait_result.message}"
        except Exception as exc:
            logger.warning(
                "Failed to queue TR-069 bootstrap wait after manual bind for ONT %s: %s",
                ont_id,
                exc,
            )
            message = f"{message}; failed to queue ACS inform wait: {exc}"
    return ok, message


# ---------------------------------------------------------------------------
# WAN Configuration Setters (TR-069)
# ---------------------------------------------------------------------------


def set_pppoe_credentials(
    db: Session,
    ont_id: str,
    *,
    username: str,
    password: str,
    instance_index: int = 1,
    ensure_instance: bool = True,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Push PPPoE credentials to ONT via TR-069."""
    from app.services.network.ont_action_wan import (
        set_pppoe_credentials as _set_pppoe_credentials,
    )

    result = _set_pppoe_credentials(
        db,
        ont_id,
        username=username,
        password=password,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=wan_vlan,
    )

    if result.success:
        ont = db.get(OntUnit, ont_id)
        if ont:
            # Store PPPoE credentials as overrides
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.pppoe_username",
                value=username,
                reason="config_setters.set_pppoe_credentials",
            )
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.pppoe_password",
                value=encrypt_credential(password),
                reason="config_setters.set_pppoe_credentials",
            )
            db.flush()
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_pppoe_credentials_tr069",
            {
                "username": username,
                "password_set": True,
                "instance_index": instance_index,
                "wan_vlan": wan_vlan,
            },
        )
        from app.services.events import emit_event
        from app.services.events.types import EventType

        emit_event(
            db,
            EventType.ont_pppoe_credentials_set,
            {
                "ont_id": ont_id,
                "ont_serial": ont.serial_number if ont else None,
                "wan_mode": "pppoe",
                "pppoe_username": username,
                "method": "tr069",
                "result": "success",
            },
            actor=actor_name_from_request(request),
        )

    _log_action_audit(
        db,
        request=request,
        action="set_pppoe_credentials",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "username": username,
            "instance_index": instance_index,
            "wan_vlan": wan_vlan,
        },
    )
    return result


def set_wan_dhcp(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    ensure_instance: bool = True,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Configure WAN for DHCP mode via TR-069."""
    from app.services.network.ont_action_wan import set_wan_dhcp as _set_wan_dhcp

    result = _set_wan_dhcp(
        db,
        ont_id,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=wan_vlan,
    )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_wan_dhcp_tr069",
            {
                "instance_index": instance_index,
                "wan_vlan": wan_vlan,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_dhcp",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "instance_index": instance_index,
            "wan_vlan": wan_vlan,
        },
    )
    return result


def set_wan_static(
    db: Session,
    ont_id: str,
    *,
    ip_address: str,
    subnet_mask: str,
    gateway: str,
    dns_servers: list[str] | None = None,
    instance_index: int = 1,
    request: Request | None = None,
) -> ActionResult:
    """Configure WAN for static IP mode via TR-069."""
    from app.services.network.ont_action_wan import set_wan_static as _set_wan_static

    result = _set_wan_static(
        db,
        ont_id,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
    )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_wan_static_tr069",
            {
                "ip_address": ip_address,
                "subnet_mask": subnet_mask,
                "gateway": gateway,
                "dns_servers": dns_servers,
                "instance_index": instance_index,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_static",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "ip_address": ip_address,
            "instance_index": instance_index,
        },
    )
    return result


def set_wan_config(
    db: Session,
    ont_id: str,
    *,
    wan_mode: str,
    pppoe_username: str | None = None,
    pppoe_password: str | None = None,
    ip_address: str | None = None,
    subnet_mask: str | None = None,
    gateway: str | None = None,
    dns_servers: list[str] | None = None,
    instance_index: int = 1,
    ensure_instance: bool = True,
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Unified WAN configuration via TR-069."""
    from app.services.network.ont_action_wan import set_wan_config as _set_wan_config

    ont = db.get(OntUnit, ont_id)
    config_pack_wan_vlan: int | None = None
    if ont and ont.olt_device_id:
        config_pack = resolve_olt_config_pack(db, ont.olt_device_id)
        if config_pack and config_pack.internet_vlan:
            config_pack_wan_vlan = config_pack.internet_vlan.tag

    wan_mode_normalized = wan_mode.strip().lower()
    resolved_wan_vlan = config_pack_wan_vlan
    if wan_mode_normalized in {"pppoe", "dhcp", "static"} and resolved_wan_vlan is None:
        result = ActionResult(
            success=False,
            message="OLT config pack internet VLAN is required before applying WAN config.",
        )
        _log_action_audit(
            db,
            request=request,
            action="set_wan_config",
            ont_id=ont_id,
            metadata={
                "success": False,
                "wan_mode": wan_mode,
                "missing_config_pack_vlan": True,
            },
        )
        return result

    result = _set_wan_config(
        db,
        ont_id,
        wan_mode=wan_mode,
        pppoe_username=pppoe_username,
        pppoe_password=pppoe_password,
        ip_address=ip_address,
        subnet_mask=subnet_mask,
        gateway=gateway,
        dns_servers=dns_servers,
        instance_index=instance_index,
        ensure_instance=ensure_instance,
        wan_vlan=resolved_wan_vlan,
    )

    if result.success:
        if ont:
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.mode",
                value=wan_mode_normalized,
                reason="config_setters.set_wan_config",
            )
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.instance_index",
                value=instance_index,
                reason="config_setters.set_wan_config",
            )
        if ont and wan_mode_normalized == "pppoe" and pppoe_username and pppoe_password:
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.pppoe_username",
                value=pppoe_username,
                reason="config_setters.set_wan_config",
            )
            upsert_ont_desired_config_value(
                db,
                ont=ont,
                field_name="wan.pppoe_password",
                value=encrypt_credential(pppoe_password),
                reason="config_setters.set_wan_config",
            )
        if ont and wan_mode_normalized == "static":
            for field_name, value in {
                "wan.ip_address": ip_address,
                "wan.subnet_mask": subnet_mask,
                "wan.gateway": gateway,
                "wan.dns_servers": dns_servers,
            }.items():
                upsert_ont_desired_config_value(
                    db,
                    ont=ont,
                    field_name=field_name,
                    value=value,
                    reason="config_setters.set_wan_config",
                )
        if ont:
            db.flush()
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_wan_config_tr069",
            {
                "wan_mode": wan_mode,
                "pppoe_username": pppoe_username,
                "password_set": bool(pppoe_password),
                "ip_address": ip_address,
                "instance_index": instance_index,
                "wan_vlan": resolved_wan_vlan,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_wan_config",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "wan_mode": wan_mode,
            "instance_index": instance_index,
            "wan_vlan": resolved_wan_vlan,
        },
    )
    return result


def probe_wan_instance(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_mode: str = "pppoe",
    request: Request | None = None,
) -> ActionResult:
    """Probe whether a WAN instance exists on the ONT."""
    from app.services.network.ont_action_wan import (
        probe_wan_instance as _probe_wan_instance,
    )

    result = _probe_wan_instance(
        db,
        ont_id,
        instance_index=instance_index,
        wan_mode=wan_mode,
    )

    _log_action_audit(
        db,
        request=request,
        action="probe_wan_instance",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "instance_index": instance_index,
            "wan_mode": wan_mode,
            "data": result.data,
        },
    )
    return result


def ensure_wan_instance(
    db: Session,
    ont_id: str,
    *,
    instance_index: int = 1,
    wan_mode: str = "pppoe",
    wan_vlan: int | None = None,
    request: Request | None = None,
) -> ActionResult:
    """Ensure a WAN instance exists on the ONT, creating if needed."""
    from app.services.network.ont_action_wan import (
        ensure_wan_instance as _ensure_wan_instance,
    )

    result = _ensure_wan_instance(
        db,
        ont_id,
        instance_index=instance_index,
        wan_mode=wan_mode,
        wan_vlan=wan_vlan,
    )

    _log_action_audit(
        db,
        request=request,
        action="ensure_wan_instance",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "waiting": result.waiting,
            "instance_index": instance_index,
            "wan_mode": wan_mode,
            "wan_vlan": wan_vlan,
        },
    )
    return result


def set_http_management(
    db: Session,
    ont_id: str,
    *,
    enabled: bool,
    port: int = 80,
    request: Request | None = None,
) -> ActionResult:
    """Enable or disable HTTP management interface via TR-069."""
    from app.services.network.ont_action_wan import (
        set_http_management as _set_http_management,
    )

    result = _set_http_management(
        db,
        ont_id,
        enabled=enabled,
        port=port,
    )

    if result.success:
        _persist_ont_plan_step(
            db,
            ont_id,
            "set_http_management_tr069",
            {
                "enabled": enabled,
                "port": port,
            },
        )

    _log_action_audit(
        db,
        request=request,
        action="set_http_management",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "enabled": enabled,
            "port": port,
        },
    )
    return result


def normalize_wan_structure(
    db: Session,
    ont_id: str,
    *,
    preserve_mgmt: bool = True,
    request: Request | None = None,
) -> ActionResult:
    """Normalize WAN structure to standard layout via TR-069.

    Deletes non-management WAN instances to establish consistent WCD layout:
    - WCD1 = Management (TR-069, static IP)
    - WCD2 = Internet (PPPoE/DHCP)

    Args:
        db: Database session.
        ont_id: OntUnit ID.
        preserve_mgmt: If True, preserve the management WAN service.
        request: Optional request for audit logging.

    Returns:
        ActionResult with normalization details.
    """
    from app.services.network.ont_action_wan import (
        normalize_wan_structure as _normalize_wan_structure,
    )

    result = _normalize_wan_structure(
        db,
        ont_id,
        preserve_mgmt=preserve_mgmt,
    )

    _log_action_audit(
        db,
        request=request,
        action="normalize_wan_structure",
        ont_id=ont_id,
        metadata={
            "success": result.success,
            "preserve_mgmt": preserve_mgmt,
            "data": result.data,
        },
    )
    return result
