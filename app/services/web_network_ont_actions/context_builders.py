"""Context builders for ONT web action UI tabs."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OntAssignment
from app.models.tr069 import Tr069CpeDevice
from app.services import network as network_service
from app.services.service_intent_ui_adapter import service_intent_ui_adapter
from app.services.web_network_ont_actions._common import (
    _display_olt_value,
)


def _enum_value(value: object) -> str | None:
    raw = getattr(value, "value", value)
    return str(raw) if raw not in (None, "") else None


def _first_present(*values: object) -> object | None:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _plan_section(ont_plan: dict[str, object], step_name: str) -> dict[str, object]:
    value = ont_plan.get(step_name)
    return value if isinstance(value, dict) else {}


def _vlan_tag(vlan: object | None) -> str:
    tag = getattr(vlan, "tag", None)
    return str(tag) if tag not in (None, "") else ""


def _desired_config_context(
    ont: object,
    *,
    ont_plan: dict[str, object],
    initial_iphost_form: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return durable desired config values for ONT config partials."""
    mgmt_plan = _plan_section(ont_plan, "configure_management_ip")
    wan_plan = _plan_section(ont_plan, "configure_wan_tr069")
    pppoe_plan = _plan_section(ont_plan, "push_pppoe_tr069") or _plan_section(
        ont_plan, "push_pppoe_omci"
    )
    lan_plan = _plan_section(ont_plan, "configure_lan_tr069")
    wifi_plan = _plan_section(ont_plan, "configure_wifi_tr069")
    initial_iphost_form = initial_iphost_form or {}

    wan_mode = _first_present(
        _enum_value(getattr(ont, "wan_mode", None)),
        wan_plan.get("wan_mode"),
    )
    wan_vlan = _first_present(
        _vlan_tag(getattr(ont, "wan_vlan", None)),
        wan_plan.get("wan_vlan"),
        pppoe_plan.get("vlan_id"),
    )

    mgmt_mode = _first_present(
        _enum_value(getattr(ont, "mgmt_ip_mode", None)),
        mgmt_plan.get("ip_mode"),
        initial_iphost_form.get("ip_mode"),
    )
    if mgmt_mode == "static_ip":
        mgmt_mode = "static"
    mgmt_vlan = _first_present(
        _vlan_tag(getattr(ont, "mgmt_vlan", None)),
        mgmt_plan.get("vlan_id"),
        initial_iphost_form.get("vlan_id"),
    )
    mgmt_ip = _first_present(
        getattr(ont, "mgmt_ip_address", None),
        mgmt_plan.get("ip_address"),
        initial_iphost_form.get("ip_address"),
    )

    return {
        "desired_mgmt_config": {
            "ip_mode": mgmt_mode or "",
            "vlan_id": str(mgmt_vlan or ""),
            "ip_address": str(mgmt_ip or ""),
            "subnet": str(
                mgmt_plan.get("subnet") or initial_iphost_form.get("subnet") or ""
            ),
            "gateway": str(
                mgmt_plan.get("gateway") or initial_iphost_form.get("gateway") or ""
            ),
        },
        "desired_wan_config": {
            "wan_mode": wan_mode or "",
            "wan_vlan": str(wan_vlan or ""),
            "ip_address": str(wan_plan.get("ip_address") or ""),
            "subnet_mask": str(wan_plan.get("subnet_mask") or ""),
            "gateway": str(wan_plan.get("gateway") or ""),
            "dns_servers": str(wan_plan.get("dns_servers") or ""),
            "instance_index": wan_plan.get("instance_index") or 1,
            "pppoe_username": str(
                getattr(ont, "pppoe_username", None)
                or pppoe_plan.get("username")
                or ""
            ),
        },
        "desired_lan_config": {
            "lan_ip": str(
                getattr(ont, "lan_gateway_ip", None) or lan_plan.get("lan_ip") or ""
            ),
            "lan_subnet": str(
                getattr(ont, "lan_subnet_mask", None)
                or lan_plan.get("lan_subnet")
                or ""
            ),
            "dhcp_enabled": _first_present(
                getattr(ont, "lan_dhcp_enabled", None),
                lan_plan.get("dhcp_enabled"),
            ),
            "dhcp_start": str(
                getattr(ont, "lan_dhcp_start", None)
                or lan_plan.get("dhcp_start")
                or ""
            ),
            "dhcp_end": str(
                getattr(ont, "lan_dhcp_end", None) or lan_plan.get("dhcp_end") or ""
            ),
        },
        "desired_wifi_config": {
            "enabled": _first_present(
                getattr(ont, "wifi_enabled", None),
                wifi_plan.get("enabled"),
            ),
            "ssid": str(getattr(ont, "wifi_ssid", None) or wifi_plan.get("ssid") or ""),
            "channel": str(
                getattr(ont, "wifi_channel", None) or wifi_plan.get("channel") or ""
            ),
            "security_mode": str(
                getattr(ont, "wifi_security_mode", None)
                or wifi_plan.get("security_mode")
                or ""
            ),
        },
    }


def iphost_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build management IP config context for the ONT detail partial."""
    shared = _load_ont_detail_config_state(db, ont_id)
    observed_state = shared["observed_state"]

    context = {
        "ont": shared["ont"],
        "iphost_config": observed_state["iphost_config"],
        "iphost_ok": observed_state["iphost_result"].ok,
        "iphost_msg": observed_state["iphost_result"].message,
        "iphost_freshness": observed_state["iphost_result"].freshness,
        "initial_iphost_form": observed_state["initial_form"],
        "vlans": observed_state["vlans"],
        "tr069_profiles": observed_state["tr069_profiles"],
        "tr069_profiles_error": observed_state["tr069_profiles_error"],
        "tr069_profiles_freshness": observed_state["profiles_result"].freshness,
    }
    context.update(
        _desired_config_context(
            shared["ont"],
            ont_plan={},
            initial_iphost_form=context["initial_iphost_form"],
        )
    )
    return context


def _empty_observed_read_result(*, message: str, data: object) -> object:
    from app.services.olt_observed_state_adapter import ObservedReadResult

    return ObservedReadResult(
        ok=True,
        message=message,
        data=data,
        source="db",
        fetched_at=None,
        stale=True,
    )


def _resolve_linked_tr069_device(db: Session, ont: object) -> object | None:
    return (
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


def _load_unified_observed_state(db: Session, ont: object) -> dict[str, object]:
    from app.services import web_network_onts as web_network_onts_service
    from app.services.network import ont_web_forms as ont_web_forms_service
    from app.services.olt_observed_state_adapter import (
        get_cached_iphost_config,
        get_cached_tr069_profiles_for_olt,
    )

    iphost_result = get_cached_iphost_config(ont) or _empty_observed_read_result(
        message="No cached IPHOST configuration.",
        data={},
    )
    iphost_config = dict(getattr(iphost_result, "data", {}) or {})
    vlans = web_network_onts_service.get_vlans_for_ont(db, ont)
    olt = web_network_onts_service.resolve_ont_connected_olt(db, ont)
    profiles_result = (
        get_cached_tr069_profiles_for_olt(olt)
        if olt is not None
        else _empty_observed_read_result(
            message="No OLT is assigned to this ONT.",
            data=[],
        )
    )
    initial_form = ont_web_forms_service.initial_iphost_form(ont, iphost_config)
    return {
        "iphost_result": iphost_result,
        "iphost_config": iphost_config,
        "vlans": vlans,
        "profiles_result": profiles_result,
        "tr069_profiles": list(getattr(profiles_result, "data", []) or []),
        "tr069_profiles_error": (
            profiles_result.message
            if (not profiles_result.ok or profiles_result.stale)
            else None
        ),
        "initial_form": initial_form,
    }


def _load_subscriber_info(db: Session, ont: object) -> dict[str, object]:
    assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()
    if not assignment or not assignment.subscriber_id:
        return {}
    db.scalars(
        select(Subscription)
        .where(Subscription.subscriber_id == assignment.subscriber_id)
        .where(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    ).first()
    if assignment.subscriber:
        return {
            "name": str(
                getattr(assignment.subscriber, "display_name", "")
                or getattr(assignment.subscriber, "full_name", "")
                or ""
            ).strip()
        }
    return {}


def _load_ont_detail_config_state(db: Session, ont_id: str) -> dict[str, object]:
    from app.services import web_network_core_devices_views as core_devices_views

    ont = network_service.ont_units.get_including_inactive(db=db, entity_id=ont_id)
    linked_tr069 = _resolve_linked_tr069_device(db, ont)
    observed_state = _load_unified_observed_state(db, ont)
    detail_payload = core_devices_views.ont_detail_page_data(db, ont_id)
    detail_payload = detail_payload if isinstance(detail_payload, dict) else {}
    subscriber_info = detail_payload.get("subscriber_info", {})
    if not isinstance(subscriber_info, dict) or not subscriber_info:
        subscriber_info = _load_subscriber_info(db, ont)
    ont_plan = service_intent_ui_adapter.load_ont_plan_for_ont(db, ont_id=ont_id)
    acs_observed_intent = _build_db_observed_service_intent(linked_tr069, ont)
    return {
        "ont": ont,
        "linked_tr069": linked_tr069,
        "observed_state": observed_state,
        "detail_payload": detail_payload,
        "subscriber_info": subscriber_info,
        "ont_plan": ont_plan,
        "acs_observed_intent": acs_observed_intent,
    }


def _build_db_observed_service_intent(linked_tr069: object, ont: object) -> dict[str, object]:
    return service_intent_ui_adapter.build_acs_observed_service_intent(
        SimpleNamespace(
            available=bool(linked_tr069),
            source="db",
            fetched_at=getattr(ont, "observed_runtime_updated_at", None),
            system={
                "SerialNumber": getattr(ont, "serial_number", None),
                "MACAddress": getattr(ont, "mac_address", None),
            },
            wan={
                "pppoe_username": getattr(ont, "pppoe_username", None),
                "wan_ip": getattr(ont, "observed_wan_ip", None),
                "status": getattr(ont, "observed_pppoe_status", None),
                "wan_mode": _enum_value(getattr(ont, "wan_mode", None)),
            },
            lan={
                "lan_ip": getattr(ont, "lan_gateway_ip", None),
                "lan_subnet": getattr(ont, "lan_subnet_mask", None),
                "dhcp_enabled": getattr(ont, "lan_dhcp_enabled", None),
                "dhcp_start": getattr(ont, "lan_dhcp_start", None),
                "dhcp_end": getattr(ont, "lan_dhcp_end", None),
            },
            wireless={
                "SSID": getattr(ont, "wifi_ssid", None),
                "Enable": getattr(ont, "wifi_enabled", None),
                "Channel": getattr(ont, "wifi_channel", None),
                "Security Mode": getattr(ont, "wifi_security_mode", None),
            },
            ethernet_ports=[],
            lan_hosts=[],
        )
    )


def _unified_summary_context(
    *,
    desired_mgmt: dict[str, object],
    desired_wan: dict[str, object],
    desired_wifi: dict[str, object],
    acs_observed_intent: dict[str, object],
) -> dict[str, object]:
    observed = acs_observed_intent.get("observed", {})
    observed_wan = observed.get("wan", {}) if isinstance(observed, dict) else {}
    observed_wifi = observed.get("wifi", {}) if isinstance(observed, dict) else {}
    wan_status = (
        str(observed_wan.get("status") or "").strip().lower()
        if isinstance(observed_wan, dict)
        else ""
    )
    return {
        "mgmt_ip_summary": {
            "mode": desired_mgmt.get("ip_mode"),
            "vlan": desired_mgmt.get("vlan_id"),
            "ip": (
                desired_mgmt.get("ip_address")
                if desired_mgmt.get("ip_mode") == "static"
                else None
            ),
        },
        "wan_summary": {
            "pppoe_user": (
                observed_wan.get("pppoe_username")
                if isinstance(observed_wan, dict)
                else None
            )
            or desired_wan.get("pppoe_username"),
            "wan_ip": (
                observed_wan.get("wan_ip")
                if isinstance(observed_wan, dict)
                else None
            ),
            "status": wan_status or None,
        },
        "wifi_summary": {
            "ssid": (
                observed_wifi.get("ssid") if isinstance(observed_wifi, dict) else None
            )
            or desired_wifi.get("ssid")
        },
    }


def _observed_config_freshness(observed_intent: dict[str, object]) -> dict[str, object] | None:
    if not observed_intent:
        return None
    fetched_at = observed_intent.get("fetched_at")
    source = str(observed_intent.get("source") or "db")
    return {
        "source": source,
        "fetched_at": fetched_at,
        "stale": source != "live",
    }


def unified_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build context for the unified ONT configuration partial from DB state."""
    shared = _load_ont_detail_config_state(db, ont_id)
    ont = shared["ont"]
    linked_tr069 = shared["linked_tr069"]
    observed_state = shared["observed_state"]
    subscriber_info = shared["subscriber_info"]
    ont_plan = shared["ont_plan"]
    service_intent = service_intent_ui_adapter.build_ont_service_intent(
        ont,
        db=db,
        subscriber_info=subscriber_info,
        ont_plan=ont_plan,
    )
    acs_observed_intent = shared["acs_observed_intent"]

    context = {
        "ont": ont,
        "service_intent": service_intent,
        "acs_observed_intent": acs_observed_intent,
        "ont_plan": ont_plan,
        "iphost_config": observed_state["iphost_config"],
        "iphost_ok": True,
        "iphost_msg": observed_state["iphost_result"].message,
        "iphost_freshness": observed_state["iphost_result"].freshness,
        "initial_iphost_form": observed_state["initial_form"],
        "vlans": observed_state["vlans"],
        "tr069_profiles": observed_state["tr069_profiles"],
        "tr069_profiles_error": observed_state["tr069_profiles_error"],
        "tr069_profiles_freshness": observed_state["profiles_result"].freshness,
        "has_tr069": bool(
            linked_tr069 and str(getattr(linked_tr069, "genieacs_device_id", "") or "")
        ),
    }
    context.update(
        _desired_config_context(
            ont,
            ont_plan=ont_plan,
            initial_iphost_form=observed_state["initial_form"],
        )
    )
    context.update(
        _unified_summary_context(
            desired_mgmt=context["desired_mgmt_config"],
            desired_wan=context["desired_wan_config"],
            desired_wifi=context["desired_wifi_config"],
            acs_observed_intent=acs_observed_intent,
        )
    )
    return context


def wan_config_context(db: Session, ont_id: str) -> dict[str, object]:
    shared = _load_ont_detail_config_state(db, ont_id)
    ont = shared["ont"]
    ont_plan = shared["ont_plan"]
    observed_intent = shared["acs_observed_intent"]
    observed_state = shared["observed_state"]
    observed = observed_intent.get("observed", {})
    wan = observed.get("wan", {}) if isinstance(observed, dict) else {}
    context = {
        "ont_id": ont_id,
        "tr069_available": bool(observed_intent.get("available")),
        "ont": ont,
        "ont_plan": ont_plan,
        "acs_observed_intent": observed_intent,
        "wan_info": wan,
        "current_pppoe_user": (wan or {}).get("pppoe_username"),
        "vlans": observed_state["vlans"],
        "observed_config_freshness": _observed_config_freshness(observed_intent),
    }
    context.update(_desired_config_context(ont, ont_plan=ont_plan))
    desired_wan = context["desired_wan_config"]
    context["current_pppoe_user"] = (
        context["current_pppoe_user"] or desired_wan.get("pppoe_username")
    )
    return context


def wifi_config_context(db: Session, ont_id: str) -> dict[str, object]:
    shared = _load_ont_detail_config_state(db, ont_id)
    ont = shared["ont"]
    ont_plan = shared["ont_plan"]
    observed_intent = shared["acs_observed_intent"]
    observed = observed_intent.get("observed", {})
    wireless = observed.get("wifi", {}) if isinstance(observed, dict) else {}
    context = {
        "ont_id": ont_id,
        "tr069_available": bool(observed_intent.get("available")),
        "acs_observed_intent": observed_intent,
        "ont_plan": ont_plan,
        "wireless_info": wireless,
        "current_ssid": (wireless or {}).get("ssid"),
        "observed_config_freshness": _observed_config_freshness(observed_intent),
    }
    context.update(_desired_config_context(ont, ont_plan=ont_plan))
    desired_wifi = context["desired_wifi_config"]
    context["current_ssid"] = context["current_ssid"] or desired_wifi.get("ssid")
    return context


def tr069_profile_config_context(db: Session, ont_id: str) -> dict[str, object]:
    shared = _load_ont_detail_config_state(db, ont_id)
    ont = shared["ont"]
    observed_state = shared["observed_state"]
    profiles_result = observed_state["profiles_result"]
    current_profile, current_profile_error = (
        service_intent_ui_adapter.resolve_effective_tr069_profile(db, ont=ont)
    )
    return {
        "ont_id": ont_id,
        "tr069_profiles": observed_state["tr069_profiles"],
        "tr069_profiles_error": (
            observed_state["tr069_profiles_error"] or current_profile_error
        ),
        "tr069_profiles_freshness": profiles_result.freshness,
        "current_profile": getattr(current_profile, "profile_name", None)
        or getattr(current_profile, "name", None),
        "current_profile_id": getattr(current_profile, "profile_id", None),
    }


def lan_config_context(db: Session, ont_id: str) -> dict[str, object]:
    shared = _load_ont_detail_config_state(db, ont_id)
    ont = shared["ont"]
    ont_plan = shared["ont_plan"]
    observed_intent = shared["acs_observed_intent"]
    observed = observed_intent.get("observed", {})
    observed = observed if isinstance(observed, dict) else {}
    context = {
        "ont_id": ont_id,
        "tr069_available": bool(observed_intent.get("available")),
        "acs_observed_intent": observed_intent,
        "ont_plan": ont_plan,
        "lan_info": observed.get("lan", {}),
        "ethernet_ports": observed.get("ethernet_ports", []),
        "lan_hosts": observed.get("lan_hosts", []),
        "observed_config_freshness": _observed_config_freshness(observed_intent),
    }
    context.update(_desired_config_context(ont, ont_plan=ont_plan))
    return context


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
