"""Build normalized ONT service intent for operator-facing views."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


def _value(raw: object, default: str = "Not set") -> str:
    text = str(raw or "").strip()
    return text or default


def _enum_value(raw: object) -> str | None:
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    return str(value) if value is not None else None


def _vlan_label(vlan: object | None) -> str:
    if vlan is None:
        return "Not set"
    tag = getattr(vlan, "tag", None)
    name = getattr(vlan, "name", None)
    if tag and name:
        return f"VLAN {tag} - {name}"
    if tag:
        return f"VLAN {tag}"
    return _value(name)


def _plan_section(ont_plan: dict[str, Any], step: str) -> dict[str, Any]:
    value = ont_plan.get(step)
    return value if isinstance(value, dict) else {}


def build_service_intent(
    ont: object,
    *,
    subscriber_info: dict[str, object] | None = None,
    ont_plan: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Return a normalized desired-service summary for an ONT.

    The ONT model contains the durable service intent we have today. Service
    order execution context fills gaps for values that are not yet first-class
    ONT columns, such as LAN DHCP ranges and WiFi settings.
    """
    subscriber_info = subscriber_info or {}
    ont_plan = ont_plan or {}
    mgmt_plan = _plan_section(ont_plan, "configure_management_ip")
    wan_plan = _plan_section(ont_plan, "configure_wan_tr069")
    lan_plan = _plan_section(ont_plan, "configure_lan_tr069")
    wifi_plan = _plan_section(ont_plan, "configure_wifi_tr069")
    service_port_plan = _plan_section(ont_plan, "create_service_port")
    pppoe_plan = _plan_section(ont_plan, "push_pppoe_tr069")

    onu_mode = _enum_value(getattr(ont, "onu_mode", None)) or "routing"
    wan_mode = _enum_value(getattr(ont, "wan_mode", None)) or wan_plan.get("wan_mode")
    mgmt_ip_mode = (
        _enum_value(getattr(ont, "mgmt_ip_mode", None))
        or mgmt_plan.get("ip_mode")
        or "dhcp"
    )
    mgmt_ip_mode = "static" if mgmt_ip_mode == "static_ip" else str(mgmt_ip_mode)

    pppoe_username = getattr(ont, "pppoe_username", None) or pppoe_plan.get("username")
    static_ip = wan_plan.get("ip_address")
    static_gateway = wan_plan.get("gateway")
    static_dns = wan_plan.get("dns_servers")

    internet_method = "Bridge" if onu_mode == "bridging" else _value(wan_mode, "Not set")

    sections = [
        {
            "key": "management",
            "title": "Management Plane",
            "description": "How operators and ACS reach the ONT.",
            "rows": [
                {"label": "Management VLAN", "value": _vlan_label(getattr(ont, "mgmt_vlan", None))},
                {"label": "Management IP Method", "value": _value(mgmt_ip_mode).replace("_", " ").title()},
                {
                    "label": "Management IP",
                    "value": _value(
                        getattr(ont, "mgmt_ip_address", None)
                        or mgmt_plan.get("ip_address")
                    ),
                },
                {"label": "Gateway", "value": _value(mgmt_plan.get("gateway"))},
            ],
        },
        {
            "key": "internet",
            "title": "Internet Service",
            "description": "How subscriber traffic is delivered.",
            "rows": [
                {"label": "Internet VLAN", "value": _vlan_label(getattr(ont, "wan_vlan", None))},
                {"label": "ONU Mode", "value": _value(onu_mode).replace("_", " ").title()},
                {"label": "WAN Method", "value": internet_method.replace("_", " ").title()},
                {"label": "PPPoE User", "value": _value(pppoe_username)},
                {"label": "Static IP", "value": _value(static_ip)},
                {"label": "Gateway / DNS", "value": " / ".join(v for v in [_value(static_gateway, ""), _value(static_dns, "")] if v) or "Not set"},
            ],
        },
        {
            "key": "lan",
            "title": "Subscriber LAN",
            "description": "The customer-side gateway and DHCP server.",
            "rows": [
                {"label": "LAN Gateway", "value": _value(lan_plan.get("lan_ip"))},
                {"label": "LAN Subnet", "value": _value(lan_plan.get("lan_subnet"))},
                {
                    "label": "DHCP Server",
                    "value": "Enabled"
                    if lan_plan.get("dhcp_enabled") is True
                    else "Disabled"
                    if lan_plan.get("dhcp_enabled") is False
                    else "Not set",
                },
                {
                    "label": "DHCP Range",
                    "value": " - ".join(
                        v for v in [_value(lan_plan.get("dhcp_start"), ""), _value(lan_plan.get("dhcp_end"), "")] if v
                    )
                    or "Not set",
                },
            ],
        },
        {
            "key": "wifi",
            "title": "WiFi",
            "description": "Managed wireless settings for WiFi-capable ONTs.",
            "rows": [
                {
                    "label": "Radio",
                    "value": "Enabled"
                    if wifi_plan.get("enabled") is True
                    else "Disabled"
                    if wifi_plan.get("enabled") is False
                    else "Not set",
                },
                {"label": "SSID", "value": _value(wifi_plan.get("ssid"))},
                {"label": "Security", "value": _value(wifi_plan.get("security_mode"))},
                {"label": "Channel", "value": _value(wifi_plan.get("channel"))},
            ],
        },
        {
            "key": "service_path",
            "title": "OLT Service Path",
            "description": "OLT-side forwarding, VLAN, and rate policy.",
            "rows": [
                {"label": "Service VLAN", "value": _value(service_port_plan.get("vlan_id") or service_port_plan.get("vlan"))},
                {"label": "GEM Index", "value": _value(service_port_plan.get("gem_index"))},
                {"label": "Tag Transform", "value": _value(service_port_plan.get("tag_transform"))},
                {
                    "label": "Subscriber",
                    "value": _value(subscriber_info.get("name")),
                },
            ],
        },
    ]

    missing = sum(
        1
        for section in sections
        for row in section["rows"]
        if row["value"] == "Not set"
    )
    return {
        "sections": sections,
        "missing_count": missing,
        "is_complete": missing == 0,
    }


def load_latest_ont_plan(db: Session, *, subscription_id: object | None = None) -> dict[str, Any]:
    """Load the latest service-order ONT plan for a subscription."""
    if subscription_id is None:
        return {}
    from app.models.provisioning import ServiceOrder

    order_stmt = (
        select(ServiceOrder)
        .where(ServiceOrder.subscription_id == subscription_id)
        .order_by(ServiceOrder.created_at.desc())
        .limit(1)
    )
    service_order = db.scalars(order_stmt).first()
    execution_context = getattr(service_order, "execution_context", None) or {}
    if not isinstance(execution_context, dict):
        return {}
    ont_plan = execution_context.get("ont_plan")
    return ont_plan if isinstance(ont_plan, dict) else {}


def load_ont_plan_for_ont(db: Session, *, ont_id: str) -> dict[str, Any]:
    """Load the latest stored ONT plan for an ONT assignment/provisioning flow."""
    from app.services import web_network_onts_provisioning as provisioning_web_service

    service_order_id = provisioning_web_service.provisioning_service.resolve_service_order_id_for_ont(
        db, ont_id
    )
    if not service_order_id:
        return {}
    order = provisioning_web_service.provisioning_service.service_orders.get(
        db, service_order_id
    )
    execution_context = getattr(order, "execution_context", None) or {}
    if not isinstance(execution_context, dict):
        return {}
    ont_plan = execution_context.get("ont_plan")
    return ont_plan if isinstance(ont_plan, dict) else {}
