"""Wireless mast management service."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.wireless_mast import WirelessMast
from app.schemas.wireless_mast import WirelessMastCreate, WirelessMastUpdate

logger = logging.getLogger(__name__)


class WirelessMastManager:
    def list(
        self,
        db: Session,
        pop_site_id: uuid.UUID | None = None,
        is_active: bool | None = None,
    ) -> list[WirelessMast]:
        stmt = select(WirelessMast).order_by(WirelessMast.name)
        if pop_site_id is not None:
            stmt = stmt.where(WirelessMast.pop_site_id == pop_site_id)
        if is_active is not None:
            stmt = stmt.where(WirelessMast.is_active == is_active)
        return list(db.scalars(stmt).all())

    def get(self, db: Session, mast_id: uuid.UUID) -> WirelessMast | None:
        result: WirelessMast | None = db.get(WirelessMast, mast_id)
        return result

    def create(self, db: Session, data: WirelessMastCreate) -> WirelessMast:
        mast = WirelessMast(
            name=data.name,
            latitude=data.latitude,
            longitude=data.longitude,
            height_m=data.height_m,
            structure_type=data.structure_type,
            owner=data.owner,
            status=data.status,
            is_active=data.is_active,
            notes=data.notes,
            metadata_=data.metadata_,
            pop_site_id=data.pop_site_id,
        )
        db.add(mast)
        db.flush()
        return mast

    def update(
        self, db: Session, mast_id: uuid.UUID, data: WirelessMastUpdate
    ) -> WirelessMast | None:
        mast = self.get(db, mast_id)
        if not mast:
            return None
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(mast, key, value)
        db.flush()
        return mast

    def delete(self, db: Session, mast_id: uuid.UUID) -> bool:
        mast = self.get(db, mast_id)
        if not mast:
            return False
        db.delete(mast)
        db.flush()
        return True


wireless_masts = WirelessMastManager()
