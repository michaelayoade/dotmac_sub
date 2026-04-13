"""OLT web resource assignment and event context helpers."""

from __future__ import annotations

import logging

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.services.network.olt_web_forms import get_olt_or_none

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VLAN / IP Pool assignment helpers
# ---------------------------------------------------------------------------


def available_vlans_for_olt(db: Session, olt_id: str) -> list:
    """Return VLANs not yet assigned to this OLT."""
    from app.models.network import Vlan

    return list(
        db.scalars(
            select(Vlan)
            .where(Vlan.olt_device_id.is_(None))
            .where(Vlan.is_active.is_(True))
            .order_by(Vlan.tag.asc())
        ).all()
    )


def available_ip_pools_for_olt(db: Session, olt_id: str) -> list:
    """Return IP pools not yet assigned to any OLT."""
    from app.models.network import IpPool

    return list(
        db.scalars(
            select(IpPool)
            .where(IpPool.olt_device_id.is_(None))
            .where(IpPool.is_active.is_(True))
            .order_by(IpPool.name.asc())
        ).all()
    )


def ip_pool_usage_summary(db: Session, pools: list) -> list[dict[str, object]]:
    """Return display-ready IPAM usage rows for OLT-scoped pools."""
    from app.models.network import IPAssignment, IPv4Address, IPVersion
    from app.services.web_network_onts_provisioning import available_static_ipv4_choices

    ipv4_pool_ids = [
        pool.id
        for pool in pools or []
        if getattr(
            getattr(pool, "ip_version", None),
            "value",
            getattr(pool, "ip_version", None),
        )
        == IPVersion.ipv4.value
    ]
    usage_by_pool: dict[str, dict[str, int]] = {}
    if ipv4_pool_ids:
        usage_rows = (
            db.query(
                IPv4Address.pool_id,
                func.count(IPv4Address.id).label("total_records"),
                func.coalesce(
                    func.sum(case((IPAssignment.id.isnot(None), 1), else_=0)),
                    0,
                ).label("assigned"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    IPv4Address.is_reserved.is_(True),
                                    IPAssignment.id.is_(None),
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("reserved"),
            )
            .outerjoin(
                IPAssignment,
                IPAssignment.ipv4_address_id == IPv4Address.id,
            )
            .filter(IPv4Address.pool_id.in_(ipv4_pool_ids))
            .group_by(IPv4Address.pool_id)
            .all()
        )
        usage_by_pool = {
            str(pool_id): {
                "total_records": int(total_records or 0),
                "assigned": int(assigned or 0),
                "reserved": int(reserved or 0),
            }
            for pool_id, total_records, assigned, reserved in usage_rows
        }

    rows: list[dict[str, object]] = []
    for pool in pools or []:
        raw_version = getattr(pool, "ip_version", None)
        pool_version = getattr(raw_version, "value", raw_version)
        if pool_version == IPVersion.ipv4.value:
            usage = usage_by_pool.get(str(pool.id), {})
            total_records = int(usage.get("total_records", 0))
            assigned = int(usage.get("assigned", 0))
            reserved = int(usage.get("reserved", 0))
            choices = available_static_ipv4_choices(
                db,
                pool_id=str(pool.id),
                limit=1,
            )
            next_available = choices.get("recommended_ip")
            available_known = max(0, total_records - assigned - reserved)
        else:
            total_records = assigned = reserved = available_known = 0
            next_available = None
        rows.append(
            {
                "pool": pool,
                "total_records": total_records,
                "assigned": assigned,
                "reserved": reserved,
                "available_known": available_known,
                "next_available": next_available,
            }
        )
    return rows


def assign_vlan_to_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Assign a VLAN to an OLT. Returns (success, message)."""
    from app.models.network import Vlan
    from app.services.network.ipam_scope import sync_ip_pool_olt_scope_for_vlan

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if vlan.olt_device_id is not None:
        return False, f"VLAN {vlan.tag} is already assigned to an OLT"
    vlan.olt_device_id = olt.id
    sync_ip_pool_olt_scope_for_vlan(db, vlan)
    db.commit()
    logger.info("Assigned VLAN %s (tag %d) to OLT %s", vlan_id, vlan.tag, olt.name)
    return True, f"VLAN {vlan.tag} assigned"


def unassign_vlan_from_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Remove VLAN assignment from an OLT."""
    from app.models.network import Vlan
    from app.services.network.ipam_scope import sync_ip_pool_olt_scope_for_vlan

    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if str(vlan.olt_device_id) != olt_id:
        return False, "VLAN is not assigned to this OLT"
    vlan.olt_device_id = None
    sync_ip_pool_olt_scope_for_vlan(db, vlan)
    db.commit()
    logger.info("Unassigned VLAN %s (tag %d) from OLT %s", vlan_id, vlan.tag, olt_id)
    return True, f"VLAN {vlan.tag} unassigned"


def assign_ip_pool_to_olt(
    db: Session, olt_id: str, pool_id: str, vlan_id: str | None = None
) -> tuple[bool, str]:
    """Assign an IP pool to an OLT."""
    from app.models.network import IpPool, Vlan

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    pool = db.get(IpPool, pool_id)
    if not pool:
        return False, "IP pool not found"
    if pool.olt_device_id is not None:
        return False, f"Pool '{pool.name}' is already assigned to an OLT"
    existing_vlan = db.get(Vlan, pool.vlan_id) if pool.vlan_id else None
    if pool.vlan_id and not existing_vlan:
        return False, "Pool VLAN scope is invalid"
    vlan_id_value = str(vlan_id or "").strip()
    if vlan_id_value:
        vlan = db.get(Vlan, vlan_id_value)
        if not vlan:
            return False, "VLAN not found"
        if str(getattr(vlan, "olt_device_id", "") or "") != str(olt.id):
            return False, "Selected VLAN is not assigned to this OLT"
        if existing_vlan and str(existing_vlan.id) != str(vlan.id):
            return False, "Pool is already scoped to a different VLAN"
        pool.vlan_id = vlan.id
    elif existing_vlan and str(
        getattr(existing_vlan, "olt_device_id", "") or ""
    ) != str(olt.id):
        return False, "Pool is scoped to a VLAN on a different OLT"
    pool.olt_device_id = olt.id
    db.commit()
    logger.info("Assigned IP pool %s (%s) to OLT %s", pool_id, pool.name, olt.name)
    return True, f"Pool '{pool.name}' assigned"


def unassign_ip_pool_from_olt(
    db: Session, olt_id: str, pool_id: str
) -> tuple[bool, str]:
    """Remove IP pool assignment from an OLT."""
    from app.models.network import IpPool

    pool = db.get(IpPool, pool_id)
    if not pool:
        return False, "IP pool not found"
    if str(pool.olt_device_id) != olt_id:
        return False, "Pool is not assigned to this OLT"
    pool.olt_device_id = None
    pool.vlan_id = None
    db.commit()
    logger.info("Unassigned IP pool %s (%s) from OLT %s", pool_id, pool.name, olt_id)
    return True, f"Pool '{pool.name}' unassigned"


def olt_device_events_context(db: Session, olt_id: str) -> dict:
    """Build context for the OLT device events tab.

    Queries ONT-related physical-link events from the EventStore where the
    payload contains this OLT's ID.

    Args:
        db: Database session.
        olt_id: OLT device ID.

    Returns:
        Dict with events list and has_more flag.
    """
    from sqlalchemy import select

    from app.models.event_store import EventStore

    ont_event_types = [
        "ont.online",
        "ont.offline",
        "ont.signal_degraded",
        "ont.discovered",
        "ont.provisioned",
        "ont.config_updated",
        "ont.moved",
    ]
    stmt = (
        select(EventStore)
        .where(
            EventStore.event_type.in_(ont_event_types),
            EventStore.payload["olt_id"].astext == olt_id,
        )
        .order_by(EventStore.created_at.desc())
        .limit(100)
    )
    events = list(db.scalars(stmt).all())
    return {"events": events, "has_more": len(events) >= 100}
