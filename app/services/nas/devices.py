"""NAS device CRUD service."""

import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    ConnectionType,
    NasDevice,
    NasDeviceStatus,
    NasVendor,
)
from app.models.network_monitoring import PopSite
from app.schemas.catalog import NasDeviceCreate, NasDeviceUpdate
from app.services.common import apply_ordering, apply_pagination, coerce_uuid
from app.services.credential_crypto import encrypt_nas_credentials
from app.services.nas._helpers import _emit_nas_event
from app.services.response import ListResponseMixin

logger = logging.getLogger(__name__)


class NasDevices(ListResponseMixin):
    """Service class for NAS device CRUD operations."""

    ALLOWED_ORDER_COLUMNS = {
        "name": NasDevice.name,
        "vendor": NasDevice.vendor,
        "status": NasDevice.status,
        "created_at": NasDevice.created_at,
        "updated_at": NasDevice.updated_at,
    }

    @staticmethod
    def create(db: Session, payload: NasDeviceCreate) -> NasDevice:
        """Create a new NAS device."""
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if provided
        if data.get("pop_site_id"):
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        device = NasDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        _emit_nas_event(
            db, "nas_device_created", {"device_id": str(device.id), "name": device.name}
        )
        return device

    @staticmethod
    def get(db: Session, device_id: str | UUID) -> NasDevice:
        """Get a NAS device by ID."""
        device_id = coerce_uuid(device_id)
        device = cast(NasDevice | None, db.get(NasDevice, device_id))
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        return device

    @staticmethod
    def get_by_code(db: Session, code: str) -> NasDevice | None:
        """Get a NAS device by its code."""
        return cast(
            NasDevice | None,
            db.execute(
                select(NasDevice).where(NasDevice.code == code)
            ).scalar_one_or_none(),
        )

    @staticmethod
    def list(
        db: Session,
        vendor: NasVendor | None = None,
        is_active: bool | None = None,
        order_by: str = "name",
        order_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        search: str | None = None,
    ) -> list[NasDevice]:
        """List NAS devices with filtering and pagination."""
        query = select(NasDevice)

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        query = apply_ordering(
            query, order_by, order_dir, NasDevices.ALLOWED_ORDER_COLUMNS
        )
        query = apply_pagination(query, limit, offset)

        return list(db.execute(query).scalars().all())

    @staticmethod
    def update(
        db: Session, device_id: str | UUID, payload: NasDeviceUpdate
    ) -> NasDevice:
        """Update a NAS device."""
        device = NasDevices.get(db, device_id)
        data = payload.model_dump(exclude_unset=True)

        # Validate pop_site if being changed
        if "pop_site_id" in data and data["pop_site_id"]:
            pop_site = db.get(PopSite, data["pop_site_id"])
            if not pop_site:
                raise HTTPException(status_code=404, detail="POP site not found")

        # Encrypt credential fields before storage
        data = encrypt_nas_credentials(data)

        for key, value in data.items():
            setattr(device, key, value)

        db.commit()
        db.refresh(device)
        _emit_nas_event(
            db, "nas_device_updated", {"device_id": str(device.id), "name": device.name}
        )
        return device

    @staticmethod
    def delete(db: Session, device_id: str | UUID) -> None:
        """Delete a NAS device."""
        device = NasDevices.get(db, device_id)
        _emit_nas_event(
            db, "nas_device_deleted", {"device_id": str(device.id), "name": device.name}
        )
        device.is_active = False
        device.status = NasDeviceStatus.decommissioned
        db.commit()

    @staticmethod
    def update_last_seen(db: Session, device_id: str | UUID) -> NasDevice:
        """Update the last_seen_at timestamp for a device."""
        device = NasDevices.get(db, device_id)
        device.last_seen_at = datetime.now(UTC)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def count(
        db: Session,
        *,
        vendor: NasVendor | None = None,
        status: NasDeviceStatus | None = None,
        connection_type: ConnectionType | None = None,
        pop_site_id: UUID | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> int:
        """Count NAS devices with filtering (same filters as list)."""
        query = select(func.count(NasDevice.id))

        if vendor:
            query = query.where(NasDevice.vendor == vendor)
        if status:
            query = query.where(NasDevice.status == status)
        if connection_type:
            query = query.where(
                NasDevice.supported_connection_types.contains([connection_type.value])
            )
        if pop_site_id:
            query = query.where(NasDevice.pop_site_id == pop_site_id)
        if is_active is None:
            query = query.where(NasDevice.is_active.is_(True))
        else:
            query = query.where(NasDevice.is_active == is_active)
        if search:
            search_pattern = f"%{search}%"
            query = query.where(
                (NasDevice.name.ilike(search_pattern))
                | (NasDevice.code.ilike(search_pattern))
                | (NasDevice.ip_address.ilike(search_pattern))
                | (NasDevice.management_ip.ilike(search_pattern))
            )

        return db.execute(query).scalar() or 0

    @staticmethod
    def count_by_vendor(db: Session) -> dict[str, int]:
        """Get count of devices grouped by vendor."""
        result = db.execute(
            select(NasDevice.vendor, func.count(NasDevice.id)).group_by(
                NasDevice.vendor
            )
        ).all()
        return {str(vendor.value): count for vendor, count in result}

    @staticmethod
    def count_by_status(db: Session) -> dict[str, int]:
        """Get count of devices grouped by status."""
        result = db.execute(
            select(NasDevice.status, func.count(NasDevice.id)).group_by(
                NasDevice.status
            )
        ).all()
        return {str(status.value): count for status, count in result}

    @staticmethod
    def get_stats(db: Session) -> dict:
        """Get combined NAS device statistics by vendor and status."""
        return {
            "by_vendor": NasDevices.count_by_vendor(db),
            "by_status": NasDevices.count_by_status(db),
        }
