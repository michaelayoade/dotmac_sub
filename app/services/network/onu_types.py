"""ONU type catalog management services."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import GponChannel, OnuCapability, OnuType, PonType
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


class OnuTypes:
    """CRUD operations for ONU type catalog entries."""

    @staticmethod
    def list(
        db: Session,
        *,
        pon_type: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> list[OnuType]:
        """List ONU types with optional filtering."""
        stmt = select(OnuType)
        if is_active is not None:
            stmt = stmt.where(OnuType.is_active.is_(is_active))
        if pon_type:
            try:
                pt = PonType(pon_type)
                stmt = stmt.where(OnuType.pon_type == pt)
            except ValueError:
                logger.warning("Invalid pon_type filter value: %s", pon_type)
        if search:
            stmt = stmt.where(OnuType.name.ilike(f"%{search}%"))

        col = getattr(OnuType, order_by, OnuType.name)
        stmt = stmt.order_by(col.desc() if order_dir == "desc" else col.asc())
        stmt = stmt.limit(limit).offset(offset)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get(db: Session, onu_type_id: str) -> OnuType:
        """Get an ONU type by ID or raise 404."""
        onu_type = db.get(OnuType, coerce_uuid(onu_type_id))
        if not onu_type:
            raise HTTPException(status_code=404, detail="ONU type not found")
        return onu_type

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        pon_type: PonType,
        gpon_channel: GponChannel,
        ethernet_ports: int = 0,
        wifi_ports: int = 0,
        voip_ports: int = 0,
        catv_ports: int = 0,
        allow_custom_profiles: bool = True,
        capability: OnuCapability,
        notes: str | None = None,
    ) -> OnuType:
        """Create a new ONU type catalog entry."""
        onu_type = OnuType(
            name=name,
            pon_type=pon_type,
            gpon_channel=gpon_channel,
            ethernet_ports=ethernet_ports,
            wifi_ports=wifi_ports,
            voip_ports=voip_ports,
            catv_ports=catv_ports,
            allow_custom_profiles=allow_custom_profiles,
            capability=capability,
            notes=notes,
        )
        db.add(onu_type)
        db.commit()
        db.refresh(onu_type)
        logger.info("Created ONU type %s: %s", onu_type.id, onu_type.name)
        return onu_type

    @staticmethod
    def update(db: Session, onu_type_id: str, **kwargs: object) -> OnuType:
        """Update an existing ONU type catalog entry."""
        onu_type = db.get(OnuType, coerce_uuid(onu_type_id))
        if not onu_type:
            raise HTTPException(status_code=404, detail="ONU type not found")
        for key, value in kwargs.items():
            if value is not None and hasattr(onu_type, key):
                setattr(onu_type, key, value)
        db.commit()
        db.refresh(onu_type)
        logger.info("Updated ONU type %s: %s", onu_type.id, onu_type.name)
        return onu_type

    @staticmethod
    def delete(db: Session, onu_type_id: str) -> None:
        """Soft-delete an ONU type by setting is_active=False."""
        onu_type = db.get(OnuType, coerce_uuid(onu_type_id))
        if not onu_type:
            raise HTTPException(status_code=404, detail="ONU type not found")
        onu_type.is_active = False
        db.commit()
        logger.info("Soft-deleted ONU type %s", onu_type_id)

    @staticmethod
    def count(db: Session, *, is_active: bool | None = None) -> int:
        """Count ONU types."""
        stmt = select(func.count()).select_from(OnuType)
        if is_active is not None:
            stmt = stmt.where(OnuType.is_active.is_(is_active))
        return db.scalar(stmt) or 0


onu_types = OnuTypes()
