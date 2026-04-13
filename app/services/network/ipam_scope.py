"""Shared IPAM scope helpers for VLAN and OLT relationships."""

from __future__ import annotations

from sqlalchemy.orm import Session


def sync_ip_pool_olt_scope_for_vlan(db: Session, vlan: object) -> int:
    """Keep pools scoped to a VLAN aligned with that VLAN's OLT scope."""
    from app.models.network import IpPool

    pools = db.query(IpPool).filter(IpPool.vlan_id == vlan.id).all()
    for pool in pools:
        pool.olt_device_id = vlan.olt_device_id
    return len(pools)
