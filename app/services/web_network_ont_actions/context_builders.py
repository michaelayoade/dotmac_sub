"""Context builders for ONT web action UI tabs."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OntAssignment
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.web_network_ont_actions._common import (
    _display_olt_value,
)
from app.services.web_network_ont_actions.diagnostics import fetch_iphost_config

logger = logging.getLogger(__name__)


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


def configure_form_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the ONT configure form (database-backed fields)."""
    from app.services import web_network_onts as web_network_onts_service

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    mgmt_ip_choices = web_network_onts_service.management_ip_choices_for_ont(db, ont)

    context = {
        "ont": ont,
        "ont_id": ont_id,
        "vlans": vlans,
        # Current values from DB
        "wan_mode": getattr(ont, "wan_mode", None),
        "wan_vlan_id": str(ont.wan_vlan_id) if ont.wan_vlan_id else "",
        "config_method": getattr(ont, "config_method", None),
        "ip_protocol": getattr(ont, "ip_protocol", None),
        "pppoe_username": ont.pppoe_username or "",
        "mgmt_ip_mode": getattr(ont, "mgmt_ip_mode", None),
        "mgmt_vlan_id": str(ont.mgmt_vlan_id) if ont.mgmt_vlan_id else "",
        "mgmt_ip_address": ont.mgmt_ip_address or "",
        "mgmt_remote_access": getattr(ont, "mgmt_remote_access", False),
        "lan_gateway_ip": ont.lan_gateway_ip or "",
        "lan_subnet_mask": ont.lan_subnet_mask or "",
        "lan_dhcp_enabled": getattr(ont, "lan_dhcp_enabled", None),
        "lan_dhcp_start": ont.lan_dhcp_start or "",
        "lan_dhcp_end": ont.lan_dhcp_end or "",
        "voip_enabled": getattr(ont, "voip_enabled", False),
    }
    context.update(mgmt_ip_choices)
    return context


def olt_side_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT config."""
    from app.services.web_network_ont_actions.operational import fetch_olt_side_config

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


def olt_status_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for OLT-side ONT status."""
    from app.services.web_network_ont_actions.operational import fetch_olt_status

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
