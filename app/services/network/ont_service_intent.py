"""Build normalized ONT service intent for operator-facing views."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.network.effective_ont_config import resolve_effective_ont_config


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


def _active_profile_wan_services(
    ont: object, db: Session | None = None
) -> list[object]:
    profile = getattr(ont, "provisioning_profile", None)
    services = getattr(profile, "wan_services", None) or []
    return [service for service in services if getattr(service, "is_active", False)]


def _active_wan_service_instances(
    ont: object, db: Session | None = None
) -> list[object]:
    """Get active WAN service instances for an ONT (Phase 2+3 architecture)."""
    ont_id = getattr(ont, "id", None)
    if db is not None and ont_id:
        from app.models.network import OntWanServiceInstance

        return list(
            db.scalars(
                select(OntWanServiceInstance)
                .where(OntWanServiceInstance.ont_id == ont_id)
                .where(OntWanServiceInstance.is_active.is_(True))
                .order_by(OntWanServiceInstance.priority, OntWanServiceInstance.name)
            ).all()
        )

    # Fallback to relationship if db not provided
    instances = getattr(ont, "wan_service_instances", None) or []
    return [inst for inst in instances if getattr(inst, "is_active", False)]


def _format_wan_service_instances(instances: list[object]) -> list[dict[str, Any]]:
    """Format WAN service instances for display in the UI."""
    result = []
    for inst in instances:
        service_type = _enum_value(getattr(inst, "service_type", None)) or "unknown"
        name = getattr(inst, "name", None) or service_type.title()
        connection_type = _enum_value(getattr(inst, "connection_type", None)) or "pppoe"
        provisioning_status = (
            _enum_value(getattr(inst, "provisioning_status", None)) or "pending"
        )

        # Build VLAN display
        s_vlan = getattr(inst, "s_vlan", None)
        c_vlan = getattr(inst, "c_vlan", None)
        vlan = getattr(inst, "vlan", None)
        vlan_tag = getattr(vlan, "tag", None) if vlan else s_vlan

        vlan_display = "Not set"
        if vlan_tag:
            vlan_display = f"VLAN {vlan_tag}"
            if c_vlan:
                vlan_display += f" (C-VLAN {c_vlan})"

        # Build credentials display
        pppoe_username = getattr(inst, "pppoe_username", None)
        credentials_display = pppoe_username if pppoe_username else "Not set"

        result.append(
            {
                "id": str(getattr(inst, "id", "")),
                "name": name,
                "service_type": service_type,
                "connection_type": connection_type.replace("_", " ").title(),
                "vlan": vlan_display,
                "pppoe_username": credentials_display,
                "nat_enabled": getattr(inst, "nat_enabled", True),
                "provisioning_status": provisioning_status,
                "last_provisioned_at": getattr(inst, "last_provisioned_at", None),
                "last_error": getattr(inst, "last_error", None),
            }
        )
    return result


def _service_label(service: object) -> str:
    return _value(
        getattr(service, "name", None)
        or _enum_value(getattr(service, "service_type", None)),
        "Service",
    )


def _profile_service_vlans(services: list[object]) -> str:
    labels: list[str] = []
    for service in services:
        service_name = _service_label(service)
        s_vlan = getattr(service, "s_vlan", None)
        c_vlan = getattr(service, "c_vlan", None)
        vlan_parts = []
        if s_vlan:
            vlan_parts.append(f"S-VLAN {s_vlan}")
        if c_vlan:
            vlan_parts.append(f"C-VLAN {c_vlan}")
        if vlan_parts:
            labels.append(f"{service_name}: {', '.join(vlan_parts)}")
    return "; ".join(labels)


def _profile_service_gems(services: list[object]) -> str:
    labels = [
        f"{_service_label(service)}: GEM {gem_port_id}"
        for service in services
        if (gem_port_id := getattr(service, "gem_port_id", None))
    ]
    return "; ".join(labels)


def _profile_service_tag_modes(services: list[object]) -> str:
    labels = []
    for service in services:
        vlan_mode = _enum_value(getattr(service, "vlan_mode", None))
        if vlan_mode:
            labels.append(f"{_service_label(service)}: {vlan_mode}")
    return "; ".join(labels)


def _service_port_value(
    service_port_plan: dict[str, Any],
    keys: tuple[str, ...],
    profile_value: object | None,
    fallback_value: object | None = None,
) -> str:
    for key in keys:
        value = service_port_plan.get(key)
        if value not in (None, "", []):
            return _value(value)
    if profile_value not in (None, "", []):
        return _value(profile_value)
    return _value(fallback_value)


from app.services.network._util import first_present

def _first_present(*values: object) -> object | None:
    return first_present(*values, exclude_empty_list=True)


def build_service_intent(
    ont: object,
    *,
    db: Session | None = None,
    subscriber_info: dict[str, object] | None = None,
    ont_plan: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Return a normalized desired-service summary for an ONT.

    The ONT model contains the durable service intent. LAN configuration is
    stored directly on the ONT (independent of service orders). Service order
    execution context provides fallback for backwards compatibility and fills
    gaps for values not yet on the ONT model, such as WiFi settings.
    """
    subscriber_info = subscriber_info or {}
    ont_plan = ont_plan or {}
    mgmt_plan = _plan_section(ont_plan, "configure_management_ip")
    lan_plan_from_order = _plan_section(ont_plan, "configure_lan_tr069")
    lan_plan = {
        "lan_ip": _first_present(
            getattr(ont, "lan_gateway_ip", None),
            lan_plan_from_order.get("lan_ip"),
        ),
        "lan_subnet": _first_present(
            getattr(ont, "lan_subnet_mask", None),
            lan_plan_from_order.get("lan_subnet"),
        ),
        "dhcp_enabled": _first_present(
            getattr(ont, "lan_dhcp_enabled", None),
            lan_plan_from_order.get("dhcp_enabled"),
        ),
        "dhcp_start": _first_present(
            getattr(ont, "lan_dhcp_start", None),
            lan_plan_from_order.get("dhcp_start"),
        ),
        "dhcp_end": _first_present(
            getattr(ont, "lan_dhcp_end", None),
            lan_plan_from_order.get("dhcp_end"),
        ),
    }
    wifi_plan_from_order = _plan_section(ont_plan, "configure_wifi_tr069")
    effective = resolve_effective_ont_config(db, ont) if db is not None else {}
    effective_values = effective.get("values", {}) if isinstance(effective, dict) else {}
    wifi_plan = {
        "enabled": _first_present(
            effective_values.get("wifi_enabled"),
            wifi_plan_from_order.get("enabled"),
        ),
        "ssid": _first_present(
            effective_values.get("wifi_ssid"),
            wifi_plan_from_order.get("ssid"),
        ),
        "channel": _first_present(
            effective_values.get("wifi_channel"),
            wifi_plan_from_order.get("channel"),
        ),
        "security_mode": _first_present(
            effective_values.get("wifi_security_mode"),
            wifi_plan_from_order.get("security_mode"),
        ),
    }
    service_port_plan = _plan_section(ont_plan, "create_service_port")
    wan_service_instances = _active_wan_service_instances(ont, db)
    profile_wan_services = wan_service_instances or _active_profile_wan_services(ont, db)
    profile_service_vlans = _profile_service_vlans(profile_wan_services)
    profile_service_gems = _profile_service_gems(profile_wan_services)
    profile_service_tag_modes = _profile_service_tag_modes(profile_wan_services)

    onu_mode = _enum_value(effective_values.get("onu_mode")) or _enum_value(
        getattr(ont, "onu_mode", None)
    )
    wan_mode = _enum_value(effective_values.get("wan_mode"))
    mgmt_ip_mode = (
        _enum_value(effective_values.get("mgmt_ip_mode"))
        or mgmt_plan.get("ip_mode")
        or ""
    )
    mgmt_ip_mode = "static" if mgmt_ip_mode == "static_ip" else str(mgmt_ip_mode)

    pppoe_username = effective_values.get("pppoe_username")

    internet_method = (
        "Bridge" if onu_mode == "bridging" else _value(wan_mode, "Not set")
    )
    normalized_wan_mode = str(wan_mode or "").strip().lower()
    internet_rows = [
        {
            "label": "Internet VLAN",
            "value": _value(
                effective_values.get("wan_vlan")
            ),
        },
        {"label": "ONU Mode", "value": _value(onu_mode).replace("_", " ").title()},
        {"label": "WAN Method", "value": internet_method.replace("_", " ").title()},
    ]
    if normalized_wan_mode == "pppoe":
        internet_rows.append({"label": "PPPoE User", "value": _value(pppoe_username)})
    elif normalized_wan_mode in {"static", "static_ip"}:
        internet_rows.extend(
            [
                {"label": "Static IP", "value": _value(effective_values.get("wan_ip"))},
                {"label": "Gateway", "value": _value(effective_values.get("gateway"))},
                {"label": "DNS", "value": _value(effective_values.get("dns_servers"))},
            ]
        )

    sections = [
        {
            "key": "management",
            "title": "Management Plane",
            "description": "How operators and ACS reach the ONT.",
            "rows": [
                {
                    "label": "Management VLAN",
                    "value": _value(effective_values.get("mgmt_vlan")),
                },
                {
                    "label": "Management IP Method",
                    "value": _value(mgmt_ip_mode).replace("_", " ").title(),
                },
                {
                    "label": "Management IP",
                    "value": _value(
                        effective_values.get("mgmt_ip_address")
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
            "rows": internet_rows,
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
                        v
                        for v in [
                            _value(lan_plan.get("dhcp_start"), ""),
                            _value(lan_plan.get("dhcp_end"), ""),
                        ]
                        if v
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
                {
                    "label": "Service VLAN",
                    "value": _service_port_value(
                        service_port_plan,
                        ("vlan_id", "vlan"),
                        profile_service_vlans,
                        effective_values.get("wan_vlan"),
                    ),
                },
                {
                    "label": "GEM Index",
                    "value": _service_port_value(
                        service_port_plan,
                        ("gem_index", "gem_port_id"),
                        profile_service_gems,
                    ),
                },
                {
                    "label": "Tag Transform",
                    "value": _service_port_value(
                        service_port_plan,
                        ("tag_transform", "vlan_mode"),
                        profile_service_tag_modes,
                    ),
                },
                {
                    "label": "Subscriber",
                    "value": _value(subscriber_info.get("name")),
                },
            ],
        },
    ]

    missing = 0
    for section in sections:
        rows = section.get("rows", [])
        if not isinstance(rows, list):
            continue
        missing += sum(
            1 for row in rows if isinstance(row, dict) and row.get("value") == "Not set"
        )

    # Include WAN service instances if available (Phase 2+3 architecture)
    formatted_instances = _format_wan_service_instances(wan_service_instances)

    return {
        "sections": sections,
        "missing_count": missing,
        "is_complete": missing == 0,
        "wan_service_instances": formatted_instances,
        "has_wan_instances": len(formatted_instances) > 0,
    }


def load_latest_ont_plan(
    db: Session, *, subscription_id: object | None = None
) -> dict[str, Any]:
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

    service_order_id = (
        provisioning_web_service.provisioning_service.resolve_service_order_id_for_ont(
            db, ont_id
        )
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
