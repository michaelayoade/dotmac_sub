"""Operational health and runbook for ONT web actions."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice, OntUnit
from app.models.tr069 import Tr069CpeDevice
from app.services.network.ont_actions import ActionResult
from app.services.network.ont_status_adapter import (
    OntStatusResult,
)
from app.services.network.ont_status_adapter import (
    get_ont_status as get_adapter_status,
)
from app.services.web_network_ont_actions._common import (
    _config_snapshot_service,
    _display_olt_value,
    _log_action_audit,
    _resolve_return_olt_context,
)
from app.services.web_network_ont_actions.diagnostics import fetch_iphost_config

logger = logging.getLogger(__name__)


def _intent_step_present(ont_plan: dict[str, Any], step_name: str) -> bool:
    section = ont_plan.get(step_name)
    return isinstance(section, dict) and any(
        value not in (None, "", []) for value in section.values()
    )


def _has_profile_service_path_intent(ont: OntUnit) -> bool:
    profile = getattr(ont, "provisioning_profile", None)
    services = getattr(profile, "wan_services", None) or []
    return any(
        getattr(service, "is_active", False)
        and (
            getattr(service, "s_vlan", None)
            or getattr(service, "c_vlan", None)
            or getattr(service, "gem_port_id", None)
        )
        for service in services
    )


def _service_path_intent_present(service_intent: dict[str, object]) -> bool:
    sections = service_intent.get("sections")
    if not isinstance(sections, list):
        return False
    for section in sections:
        if not isinstance(section, dict) or section.get("key") != "service_path":
            continue
        rows = section.get("rows")
        if not isinstance(rows, list):
            return False
        return any(
            isinstance(row, dict)
            and row.get("label") != "Subscriber"
            and row.get("value") not in (None, "", "Not set")
            for row in rows
        )
    return False


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
    raw_missing_count = service_intent.get("missing_count")
    missing_count = (
        int(raw_missing_count) if isinstance(raw_missing_count, int | str) else 0
    )
    has_service_path_intent = bool(
        _intent_step_present(ont_plan, "create_service_port")
        or _service_path_intent_present(service_intent)
        or _has_profile_service_path_intent(ont)
        or getattr(ont, "wan_vlan_id", None)
    )
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
    wan_plan = ont_plan.get("configure_wan_tr069")
    wan_plan = wan_plan if isinstance(wan_plan, dict) else {}
    raw_wan_mode = (
        wan_plan.get("wan_mode")
        or getattr(getattr(ont, "wan_mode", None), "value", None)
        or ""
    )
    wan_mode = str(raw_wan_mode).strip().lower()
    if wan_mode == "static_ip":
        wan_mode = "static"
    elif wan_mode == "setup_via_onu":
        wan_mode = "bridge"
    has_pppoe_credentials_intent = bool(
        getattr(ont, "pppoe_username", None)
        or _intent_step_present(ont_plan, "push_pppoe_tr069")
        or _intent_step_present(ont_plan, "push_pppoe_omci")
    )
    has_static_addressing_intent = bool(
        wan_plan.get("ip_address")
        and wan_plan.get("gateway")
        and wan_plan.get("dns_servers")
    )
    internet_credentials_required = wan_mode in {"pppoe", "static"}
    if wan_mode == "pppoe":
        has_internet_credentials_intent = has_pppoe_credentials_intent
    elif wan_mode == "static":
        has_internet_credentials_intent = has_static_addressing_intent
    elif wan_mode in {"dhcp", "bridge"}:
        has_internet_credentials_intent = True
    else:
        has_internet_credentials_intent = False
    has_lan_intent = bool(
        getattr(ont, "lan_gateway_ip", None)
        or getattr(ont, "lan_subnet_mask", None)
        or _intent_step_present(ont_plan, "configure_lan_tr069")
    )
    has_wifi_intent = bool(
        _intent_step_present(ont_plan, "configure_wifi_tr069")
        or getattr(ont, "wifi_enabled", None)
        or getattr(ont, "wifi_ssid", None)
        or getattr(ont, "wifi_channel", None)
        or getattr(ont, "wifi_security_mode", None)
    )
    if wan_mode == "pppoe":
        internet_credentials_message = (
            getattr(ont, "pppoe_username", None) or "No PPPoE username"
        )
    elif wan_mode == "static":
        internet_credentials_message = (
            "Static addressing intent present"
            if has_static_addressing_intent
            else "Internet credentials incomplete"
        )
    elif wan_mode in {"dhcp", "bridge"}:
        internet_credentials_message = "No separate credentials required"
    else:
        internet_credentials_message = "Set WAN method first"
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
        ),
        _runbook_step(
            order=3,
            title="OLT service path",
            source="OLT SSH",
            status="ready"
            if has_olt_context and has_service_path_intent
            else "blocked",
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
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
            message="Connection request URL captured."
            if has_cr_url
            else "No connection request URL captured yet.",
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
        ),
        _runbook_step(
            order=8,
            title="Internet credentials",
            source="ACS",
            status=(
                "complete"
                if has_acs_device and not internet_credentials_required
                else "ready"
                if has_acs_device and has_internet_credentials_intent
                else "blocked"
            ),
            message=internet_credentials_message,
            action_label="Internet credentials",
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
        ),
        _runbook_step(
            order=11,
            title="Running config snapshot",
            source="ACS read",
            status="complete"
            if has_running_snapshot
            else "ready"
            if has_acs_device
            else "blocked",
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
            action_url=f"/admin/network/onts/{ont_id}?tab=configure",
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
        from app.services.service_intent_ui_adapter import service_intent_ui_adapter

        ont_plan = service_intent_ui_adapter.load_ont_plan_for_ont(db, ont_id=ont_id)
        service_intent = (
            service_intent_ui_adapter.build_ont_service_intent(
                ont, db=db, ont_plan=ont_plan
            )
            if ont
            else {}
        )
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

    # Compute internet credentials status for health checks
    wan_plan = ont_plan.get("configure_wan_tr069")
    wan_plan = wan_plan if isinstance(wan_plan, dict) else {}
    raw_wan_mode = (
        wan_plan.get("wan_mode")
        or getattr(getattr(ont, "wan_mode", None), "value", None)
        or ""
    )
    wan_mode = str(raw_wan_mode).strip().lower()
    if wan_mode == "static_ip":
        wan_mode = "static"
    elif wan_mode == "setup_via_onu":
        wan_mode = "bridge"
    has_pppoe_credentials_intent = bool(
        getattr(ont, "pppoe_username", None)
        or _intent_step_present(ont_plan, "push_pppoe_tr069")
        or _intent_step_present(ont_plan, "push_pppoe_omci")
    )
    has_static_addressing_intent = bool(
        wan_plan.get("ip_address")
        and wan_plan.get("gateway")
        and wan_plan.get("dns_servers")
    )
    if wan_mode == "pppoe":
        has_internet_credentials_intent = has_pppoe_credentials_intent
        internet_credentials_message = (
            getattr(ont, "pppoe_username", None) or "No PPPoE username"
        )
    elif wan_mode == "static":
        has_internet_credentials_intent = has_static_addressing_intent
        internet_credentials_message = (
            "Static addressing intent present"
            if has_static_addressing_intent
            else "Internet credentials incomplete"
        )
    elif wan_mode in {"dhcp", "bridge"}:
        has_internet_credentials_intent = True
        internet_credentials_message = "No separate credentials required"
    else:
        has_internet_credentials_intent = False
        internet_credentials_message = "Set WAN method first"

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
            "label": "Internet credentials",
            "ok": bool(has_internet_credentials_intent),
            "message": internet_credentials_message,
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


def fetch_olt_side_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch ONT config/state from OLT side via SSH-backed services.

    Uses the ont_status_adapter for unified status resolution (combining
    SNMP polling data and TR-069 status), with live SSH queries for
    detailed OLT-side configuration.
    """
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
        # Use adapter for unified status (combines SNMP + TR-069 cached data)
        adapter_status: OntStatusResult = get_adapter_status(
            db, ont, include_optical=True
        )

        # Also fetch live OLT-side details via SSH for complete picture
        from app.services.network.olt_ssh_ont import get_ont_status as get_ssh_status

        ssh_ok, ssh_msg, ssh_status = get_ssh_status(olt, fsp, ont_id_on_olt)

        if ssh_ok and ssh_status:
            status_lines = [
                f"Serial Number: {_display_olt_value(ssh_status.serial_number)}",
                f"F/S/P: {fsp}",
                f"ONT-ID: {ont_id_on_olt}",
                f"Run State: {_display_olt_value(ssh_status.run_state)}",
                f"Config State: {_display_olt_value(ssh_status.config_state)}",
                f"Match State: {_display_olt_value(ssh_status.match_state)}",
            ]
            # Add unified status from adapter
            status_lines.append(f"Effective Status: {adapter_status.online_status.value}")
            status_lines.append(f"Status Source: {adapter_status.status_source.value}")
            status_lines.append(f"ACS Status: {adapter_status.acs_status.value}")
            if adapter_status.optical_metrics and adapter_status.optical_metrics.has_signal_data:
                metrics = adapter_status.optical_metrics
                if metrics.olt_rx_dbm is not None:
                    status_lines.append(f"OLT RX Power: {metrics.olt_rx_dbm} dBm")
                if metrics.onu_rx_dbm is not None:
                    status_lines.append(f"ONU RX Power: {metrics.onu_rx_dbm} dBm")
                if metrics.onu_tx_dbm is not None:
                    status_lines.append(f"ONU TX Power: {metrics.onu_tx_dbm} dBm")
            status_text = "\n".join(status_lines)
        else:
            # SSH failed but we may still have adapter status
            status_lines = [
                f"SSH Query: {ssh_msg}",
                f"Effective Status: {adapter_status.online_status.value}",
                f"Status Source: {adapter_status.status_source.value}",
                f"ACS Status: {adapter_status.acs_status.value}",
            ]
            status_text = "\n".join(status_lines)
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


def fetch_olt_status(db: Session, ont_id: str) -> dict[str, Any]:
    """Query ONT registration state using the unified status adapter.

    Uses ont_status_adapter for unified status resolution (combining SNMP
    polling data and TR-069 status), with live SSH query for GPON layer
    details (run/config/match states).

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

    # Get unified status from adapter (combines SNMP + TR-069)
    adapter_status: OntStatusResult = get_adapter_status(db, ont, include_optical=True)

    # Also get live SSH status for GPON layer details
    from app.services.network.olt_ssh_ont import get_ont_status as get_ssh_status

    ssh_ok, ssh_msg, ssh_status = get_ssh_status(olt, fsp, ont_id_on_olt)

    # Build response combining both sources
    entry: dict[str, Any] = {
        "fsp": fsp,
        "ont_id": ont_id_on_olt,
        # Unified status from adapter
        "effective_status": adapter_status.online_status.value,
        "status_source": adapter_status.status_source.value,
        "acs_status": adapter_status.acs_status.value,
    }

    # Add SSH-based GPON layer details if available
    if ssh_ok and ssh_status:
        entry.update({
            "run_state": ssh_status.run_state,
            "config_state": ssh_status.config_state,
            "match_state": ssh_status.match_state,
            "serial_number": ssh_status.serial_number,
        })
    else:
        entry.update({
            "run_state": "unknown",
            "config_state": "unknown",
            "match_state": "unknown",
            "serial_number": getattr(ont, "serial_number", None),
            "ssh_error": ssh_msg,
        })

    # Add optical metrics from adapter
    if adapter_status.optical_metrics and adapter_status.optical_metrics.has_signal_data:
        metrics = adapter_status.optical_metrics
        entry["onu_rx_signal_dbm"] = metrics.onu_rx_dbm
        entry["olt_rx_signal_dbm"] = metrics.olt_rx_dbm
        entry["onu_tx_signal_dbm"] = metrics.onu_tx_dbm
        entry["optical_source"] = metrics.source
    else:
        # Fallback to cached values on ONT model
        entry["onu_rx_signal_dbm"] = getattr(ont, "onu_rx_signal_dbm", None)
        entry["olt_rx_signal_dbm"] = getattr(ont, "olt_rx_signal_dbm", None)

    return {
        "success": True,
        "message": f"Status retrieved (source: {adapter_status.status_source.value})",
        "entry": entry,
    }
