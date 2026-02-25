"""Service helpers for admin network VLAN web routes."""

from __future__ import annotations

from app.models.network import PortVlan, VlanPurpose
from app.services import catalog as catalog_service
from app.services import network as network_service

PURPOSE_DISPLAY: dict[str, dict[str, str]] = {
    "internet": {"label": "Internet", "classes": "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400"},
    "management": {"label": "Management", "classes": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-300"},
    "tr069": {"label": "TR-069", "classes": "bg-violet-100 text-violet-800 dark:bg-violet-900/30 dark:text-violet-400"},
    "iptv": {"label": "IPTV", "classes": "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400"},
    "voip": {"label": "VoIP", "classes": "bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-400"},
    "other": {"label": "Other", "classes": "bg-slate-100 text-slate-800 dark:bg-slate-700 dark:text-slate-300"},
}

VLAN_PURPOSE_CHOICES: list[dict[str, str]] = [
    {"value": p.value, "label": PURPOSE_DISPLAY[p.value]["label"]}
    for p in VlanPurpose
]


def build_vlans_list_data(db) -> dict[str, object]:
    vlans = network_service.vlans.list(
        db=db,
        region_id=None,
        is_active=True,
        order_by="tag",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "vlans": vlans,
        "stats": {"total": len(vlans)},
        "purpose_display": PURPOSE_DISPLAY,
    }


def build_vlan_new_form_data(db) -> dict[str, object]:
    regions = catalog_service.region_zones.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    return {
        "vlan": None,
        "regions": regions,
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
    return {
        "vlan": vlan,
        "regions": regions,
        "action_url": f"/admin/network/vlans/{vlan_id}",
        "purpose_choices": VLAN_PURPOSE_CHOICES,
    }


def build_vlan_detail_data(db, *, vlan_id: str) -> dict[str, object] | None:
    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return None
    port_links = db.query(PortVlan).filter(PortVlan.vlan_id == vlan.id).all()
    return {
        "vlan": vlan,
        "port_links": port_links,
        "purpose_display": PURPOSE_DISPLAY,
    }
