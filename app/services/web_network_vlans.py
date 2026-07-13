"""Service helpers for admin network VLAN web routes."""

from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.models.network import OLTDevice, PortVlan, Vlan, VlanPurpose
from app.schemas.network import VlanCreate, VlanUpdate
from app.services import catalog as catalog_service
from app.services import network as network_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)

PURPOSE_DISPLAY: dict[str, dict[str, str]] = {
    "internet": {
        "label": "Internet",
        "classes": "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
    },
    "management": {
        "label": "Management",
        "classes": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-300",
    },
    "tr069": {
        "label": "TR-069",
        "classes": "bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-400",
    },
    "iptv": {
        "label": "IPTV",
        "classes": "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400",
    },
    "voip": {
        "label": "VoIP",
        "classes": "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400",
    },
    "other": {
        "label": "Other",
        "classes": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-300",
    },
}

VLAN_PURPOSE_CHOICES: list[dict[str, str]] = [
    {"value": p.value, "label": PURPOSE_DISPLAY[p.value]["label"]} for p in VlanPurpose
]


def _vlan_usage_counts(db, vlan_ids: list[object]) -> dict[str, dict[str, int]]:
    """Count VLAN usage across ONT desired config, WAN services, and IP pools.

    Note: ONT VLAN usage is derived from OntUnit.desired_config and WAN service
    instances, not direct OntUnit columns. This simplified view counts IP pools only.
    """
    from app.models.network import IpPool

    usage: dict[str, dict[str, int]] = {
        str(vlan_id): {"onts": 0, "wan_onts": 0, "mgmt_onts": 0, "ip_pools": 0}
        for vlan_id in vlan_ids
    }
    if not vlan_ids:
        return usage

    # ONT VLAN assignments are resolved from desired_config/WAN services elsewhere.
    # Counts remain 0 here until that source-of-truth path is indexed.

    pool_rows = db.execute(
        select(IpPool.vlan_id, func.count(IpPool.id))
        .where(IpPool.vlan_id.in_(vlan_ids))
        .group_by(IpPool.vlan_id)
    ).all()
    for vlan_id, count in pool_rows:
        usage[str(vlan_id)]["ip_pools"] = int(count or 0)

    return usage


def build_vlans_list_data(
    db: Session,
    *,
    olt_device_id: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    try:
        selected_olt_id = coerce_uuid(olt_device_id) if olt_device_id else None
    except (TypeError, ValueError):
        selected_olt_id = None
    search_filter = (search or "").strip()
    base_filters: list[ColumnElement[bool]] = [Vlan.is_active.is_(True)]
    if selected_olt_id:
        base_filters.append(Vlan.olt_device_id == selected_olt_id)

    list_filters: list[ColumnElement[bool]] = list(base_filters)
    if search_filter:
        pattern = f"%{search_filter}%"
        search_conditions: list[ColumnElement[bool]] = [
            Vlan.name.ilike(pattern),
            Vlan.description.ilike(pattern),
        ]
        if search_filter.isdigit():
            search_conditions.append(Vlan.tag == int(search_filter))
        list_filters.append(or_(*search_conditions))

    total = db.scalar(select(func.count(Vlan.id)).where(*list_filters)) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    vlans = db.scalars(
        select(Vlan)
        .options(joinedload(Vlan.olt_device))
        .where(*list_filters)
        .order_by(Vlan.tag, Vlan.name)
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    olt_devices = db.scalars(
        select(OLTDevice).where(OLTDevice.is_active.is_(True)).order_by(OLTDevice.name)
    ).all()
    selected_olt = db.get(OLTDevice, selected_olt_id) if selected_olt_id else None
    vlan_ids = [vlan.id for vlan in vlans]
    usage_counts = _vlan_usage_counts(db, vlan_ids)
    stats_row = db.execute(
        select(
            func.count(Vlan.id),
            func.count(Vlan.id).filter(Vlan.purpose == VlanPurpose.management),
            func.count(Vlan.id).filter(Vlan.purpose == VlanPurpose.internet),
        ).where(*base_filters)
    ).one()

    return {
        "vlans": vlans,
        "olt_devices": olt_devices,
        "selected_olt": selected_olt,
        "selected_olt_id": str(selected_olt_id) if selected_olt_id else "",
        "search": search_filter,
        "usage_counts": usage_counts,
        "stats": {
            "total": stats_row[0],
            "ont_refs": 0,
            "management": stats_row[1],
            "internet": stats_row[2],
        },
        "purpose_display": PURPOSE_DISPLAY,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        },
    }


def build_vlan_new_form_data(
    db, *, olt_device_id: str | None = None
) -> dict[str, object]:
    regions = catalog_service.region_zones.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    olt_devices = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    selected_olt = None
    if olt_device_id:
        try:
            selected_olt = network_service.olt_devices.get(db, olt_device_id)
        except Exception:
            selected_olt = None
    return {
        "vlan": None,
        "regions": regions,
        "olt_devices": olt_devices,
        "selected_olt": selected_olt,
        "selected_olt_id": olt_device_id or "",
        "action_url": "/admin/network/vlans",
        "purpose_choices": VLAN_PURPOSE_CHOICES,
    }


def build_vlan_edit_form_data(db, *, vlan_id: str) -> dict[str, object] | None:
    """Build context for the VLAN edit form."""
    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return None
    regions = catalog_service.region_zones.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    olt_devices = network_service.olt_devices.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    return {
        "vlan": vlan,
        "regions": regions,
        "olt_devices": olt_devices,
        "action_url": f"/admin/network/vlans/{vlan_id}",
        "purpose_choices": VLAN_PURPOSE_CHOICES,
    }


def build_vlan_detail_data(db, *, vlan_id: str) -> dict[str, object] | None:
    """Build context for VLAN detail page.

    Note: ONT VLAN usage is derived from OntUnit.desired_config and WAN service
    instances, not direct OntUnit columns.
    """
    from app.models.network import IpPool

    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return None
    port_links = db.query(PortVlan).filter(PortVlan.vlan_id == vlan.id).all()

    # ONT VLAN assignments are resolved from desired_config/WAN services elsewhere.
    # Counts remain 0 here until that source-of-truth path is indexed.
    wan_ont_count = 0
    mgmt_ont_count = 0
    ip_pool_count = (
        db.scalar(select(func.count(IpPool.id)).where(IpPool.vlan_id == vlan.id)) or 0
    )
    return {
        "vlan": vlan,
        "port_links": port_links,
        "usage_counts": {
            "onts": int(wan_ont_count) + int(mgmt_ont_count),
            "wan_onts": int(wan_ont_count),
            "mgmt_onts": int(mgmt_ont_count),
            "ip_pools": int(ip_pool_count),
        },
        "purpose_display": PURPOSE_DISPLAY,
    }


def _form_bool(form, key: str) -> bool:
    return str(form.get(key, "")).lower() in {"true", "on", "1", "yes"}


def _form_str(form, key: str) -> str:
    value = form.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _payload_from_form(form) -> dict[str, object]:
    return {
        "region_id": _form_str(form, "region_id"),
        "olt_device_id": _form_str(form, "olt_device_id"),
        "tag": int(_form_str(form, "tag") or "0"),
        "name": _form_str(form, "name") or None,
        "description": _form_str(form, "description") or None,
        "purpose": _form_str(form, "purpose") or None,
        "dhcp_snooping": _form_bool(form, "dhcp_snooping"),
        "is_active": _form_bool(form, "is_active"),
    }


def handle_vlan_create(db, form):
    payload = VlanCreate.model_validate(_payload_from_form(form))
    return network_service.vlans.create(db, payload)


def _sync_ip_pool_olt_scope_for_vlan(db, vlan) -> None:
    from app.services.network.ipam_scope import sync_ip_pool_olt_scope_for_vlan

    synced_count = sync_ip_pool_olt_scope_for_vlan(db, vlan)
    if synced_count:
        db.commit()


def handle_vlan_update(db, *, vlan_id: str, form):
    payload = VlanUpdate.model_validate(_payload_from_form(form))
    vlan = network_service.vlans.update(db, vlan_id, payload)
    _sync_ip_pool_olt_scope_for_vlan(db, vlan)
    return vlan


def handle_vlan_delete(db, *, vlan_id: str) -> None:
    from app.models.network import IpPool

    vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    db.query(IpPool).filter(IpPool.vlan_id == vlan.id).update(
        {"vlan_id": None},
        synchronize_session=False,
    )
    network_service.vlans.delete(db, vlan_id, commit=False)
    db.commit()
