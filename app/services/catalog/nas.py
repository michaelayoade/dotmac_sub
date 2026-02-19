"""NAS device management service."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, NasDeviceStatus, NasVendor
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import NasDeviceCreate, NasDeviceUpdate
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin


class NasDevices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: NasDeviceCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "vendor" not in fields_set:
            default_vendor = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_nas_vendor"
            )
            if default_vendor:
                data["vendor"] = validate_enum(
                    default_vendor, NasVendor, "vendor"
                )
        device = NasDevice(**data)
        db.add(device)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def get(db: Session, device_id: str):
        device = db.get(NasDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        return device

    @staticmethod
    def list(
        db: Session,
        vendor: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(NasDevice)
        if vendor:
            query = query.filter(
                NasDevice.vendor == validate_enum(vendor, NasVendor, "vendor")
            )
        if is_active is None:
            query = query.filter(NasDevice.is_active.is_(True))
        else:
            query = query.filter(NasDevice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": NasDevice.created_at, "name": NasDevice.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, device_id: str, payload: NasDeviceUpdate):
        device = db.get(NasDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(device, key, value)
        db.commit()
        db.refresh(device)
        return device

    @staticmethod
    def delete(db: Session, device_id: str):
        device = db.get(NasDevice, device_id)
        if not device:
            raise HTTPException(status_code=404, detail="NAS device not found")
        device.is_active = False
        device.status = NasDeviceStatus.decommissioned
        db.commit()
