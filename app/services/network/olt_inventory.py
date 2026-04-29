"""OLT inventory lookup helpers."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services import network as network_service


def get_olt_or_none(db: Session, olt_id: str) -> OLTDevice | None:
    """Get an OLT device, returning None instead of raising on 404."""
    try:
        return network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None


def active_olt_scan_targets(
    db: Session,
    *,
    olt_id: str | None = None,
) -> list[tuple[object, str]]:
    query = select(OLTDevice.id, OLTDevice.name).where(OLTDevice.is_active.is_(True))
    if olt_id:
        query = query.where(OLTDevice.id == olt_id)
    return [
        (row[0], row[1])
        for row in db.execute(query.order_by(OLTDevice.name.asc())).all()
    ]
