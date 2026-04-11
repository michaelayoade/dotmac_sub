"""OLT web resource assignment and event context helpers."""

from __future__ import annotations

import logging

from sqlalchemy import select
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


def assign_vlan_to_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Assign a VLAN to an OLT. Returns (success, message)."""
    from app.models.network import Vlan

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if vlan.olt_device_id is not None:
        return False, f"VLAN {vlan.tag} is already assigned to an OLT"
    vlan.olt_device_id = olt.id
    db.commit()
    logger.info("Assigned VLAN %s (tag %d) to OLT %s", vlan_id, vlan.tag, olt.name)
    return True, f"VLAN {vlan.tag} assigned"


def unassign_vlan_from_olt(db: Session, olt_id: str, vlan_id: str) -> tuple[bool, str]:
    """Remove VLAN assignment from an OLT."""
    from app.models.network import Vlan

    vlan = db.get(Vlan, vlan_id)
    if not vlan:
        return False, "VLAN not found"
    if str(vlan.olt_device_id) != olt_id:
        return False, "VLAN is not assigned to this OLT"
    vlan.olt_device_id = None
    db.commit()
    logger.info("Unassigned VLAN %s (tag %d) from OLT %s", vlan_id, vlan.tag, olt_id)
    return True, f"VLAN {vlan.tag} unassigned"


def assign_ip_pool_to_olt(db: Session, olt_id: str, pool_id: str) -> tuple[bool, str]:
    """Assign an IP pool to an OLT."""
    from app.models.network import IpPool

    olt = get_olt_or_none(db, olt_id)
    if not olt:
        return False, "OLT not found"
    pool = db.get(IpPool, pool_id)
    if not pool:
        return False, "IP pool not found"
    if pool.olt_device_id is not None:
        return False, f"Pool '{pool.name}' is already assigned to an OLT"
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

