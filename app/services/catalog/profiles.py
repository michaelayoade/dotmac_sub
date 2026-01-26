"""Profile and allowance management services.

Provides services for RegionZones, UsageAllowances, and SlaProfiles.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import RegionZone, SlaProfile, UsageAllowance
from app.services.common import apply_ordering, apply_pagination
from app.services.response import ListResponseMixin
from app.schemas.catalog import (
    RegionZoneCreate,
    RegionZoneUpdate,
    SlaProfileCreate,
    SlaProfileUpdate,
    UsageAllowanceCreate,
    UsageAllowanceUpdate,
)


class RegionZones(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: RegionZoneCreate):
        zone = RegionZone(**payload.model_dump())
        db.add(zone)
        db.commit()
        db.refresh(zone)
        return zone

    @staticmethod
    def get(db: Session, zone_id: str):
        zone = db.get(RegionZone, zone_id)
        if not zone:
            raise HTTPException(status_code=404, detail="Region zone not found")
        return zone

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RegionZone)
        if is_active is None:
            query = query.filter(RegionZone.is_active.is_(True))
        else:
            query = query.filter(RegionZone.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RegionZone.created_at, "name": RegionZone.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, zone_id: str, payload: RegionZoneUpdate):
        zone = db.get(RegionZone, zone_id)
        if not zone:
            raise HTTPException(status_code=404, detail="Region zone not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(zone, key, value)
        db.commit()
        db.refresh(zone)
        return zone

    @staticmethod
    def delete(db: Session, zone_id: str):
        zone = db.get(RegionZone, zone_id)
        if not zone:
            raise HTTPException(status_code=404, detail="Region zone not found")
        zone.is_active = False
        db.commit()


class UsageAllowances(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: UsageAllowanceCreate):
        allowance = UsageAllowance(**payload.model_dump())
        db.add(allowance)
        db.commit()
        db.refresh(allowance)
        return allowance

    @staticmethod
    def get(db: Session, allowance_id: str):
        allowance = db.get(UsageAllowance, allowance_id)
        if not allowance:
            raise HTTPException(status_code=404, detail="Usage allowance not found")
        return allowance

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(UsageAllowance)
        if is_active is None:
            query = query.filter(UsageAllowance.is_active.is_(True))
        else:
            query = query.filter(UsageAllowance.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageAllowance.created_at, "name": UsageAllowance.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, allowance_id: str, payload: UsageAllowanceUpdate):
        allowance = db.get(UsageAllowance, allowance_id)
        if not allowance:
            raise HTTPException(status_code=404, detail="Usage allowance not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(allowance, key, value)
        db.commit()
        db.refresh(allowance)
        return allowance

    @staticmethod
    def delete(db: Session, allowance_id: str):
        allowance = db.get(UsageAllowance, allowance_id)
        if not allowance:
            raise HTTPException(status_code=404, detail="Usage allowance not found")
        allowance.is_active = False
        db.commit()


class SlaProfiles(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: SlaProfileCreate):
        profile = SlaProfile(**payload.model_dump())
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile

    @staticmethod
    def get(db: Session, profile_id: str):
        profile = db.get(SlaProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="SLA profile not found")
        return profile

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(SlaProfile)
        if is_active is None:
            query = query.filter(SlaProfile.is_active.is_(True))
        else:
            query = query.filter(SlaProfile.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaProfile.created_at, "name": SlaProfile.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, profile_id: str, payload: SlaProfileUpdate):
        profile = db.get(SlaProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="SLA profile not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(profile, key, value)
        db.commit()
        db.refresh(profile)
        return profile

    @staticmethod
    def delete(db: Session, profile_id: str):
        profile = db.get(SlaProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="SLA profile not found")
        profile.is_active = False
        db.commit()
