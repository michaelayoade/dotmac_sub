"""OLT inventory lookup helpers."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.network import OLTDevice
from app.services import network as network_service


def get_olt_or_none(db: Session, olt_id: str) -> OLTDevice | None:
    """Get an OLT device, returning None instead of raising on 404."""
    try:
        return network_service.olt_devices.get(db=db, device_id=olt_id)
    except HTTPException:
        return None
