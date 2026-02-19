"""Service helpers for admin network VLAN web routes."""

from __future__ import annotations

from app.models.network import PortVlan
from app.services import catalog as catalog_service
from app.services import network as network_service


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
    }


def build_vlan_detail_data(db, *, vlan_id: str) -> dict[str, object] | None:
    try:
        vlan = network_service.vlans.get(db=db, vlan_id=vlan_id)
    except Exception:
        return None
    port_links = db.query(PortVlan).filter(PortVlan.vlan_id == vlan.id).all()
    return {"vlan": vlan, "port_links": port_links}
