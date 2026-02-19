"""Profile and allowance management services.

Provides services for RegionZones, UsageAllowances, and SlaProfiles.
"""

from sqlalchemy.orm import Session

from app.models.catalog import RegionZone, SlaProfile, UsageAllowance
from app.services.common import apply_ordering, apply_pagination
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state
from app.schemas.catalog import (
    RegionZoneUpdate,
    SlaProfileUpdate,
    UsageAllowanceUpdate,
)


class RegionZones(CRUDManager[RegionZone]):
    model = RegionZone
    not_found_detail = "Region zone not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_active_state(query, RegionZone.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RegionZone.created_at, "name": RegionZone.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, zone_id: str):
        return super().get(db, zone_id)

    @classmethod
    def update(cls, db: Session, zone_id: str, payload: RegionZoneUpdate):
        return super().update(db, zone_id, payload)

    @classmethod
    def delete(cls, db: Session, zone_id: str):
        return super().delete(db, zone_id)


class UsageAllowances(CRUDManager[UsageAllowance]):
    model = UsageAllowance
    not_found_detail = "Usage allowance not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_active_state(query, UsageAllowance.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": UsageAllowance.created_at, "name": UsageAllowance.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, allowance_id: str):
        return super().get(db, allowance_id)

    @classmethod
    def update(cls, db: Session, allowance_id: str, payload: UsageAllowanceUpdate):
        return super().update(db, allowance_id, payload)

    @classmethod
    def delete(cls, db: Session, allowance_id: str):
        return super().delete(db, allowance_id)


class SlaProfiles(CRUDManager[SlaProfile]):
    model = SlaProfile
    not_found_detail = "SLA profile not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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
        query = apply_active_state(query, SlaProfile.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": SlaProfile.created_at, "name": SlaProfile.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def get(cls, db: Session, profile_id: str):
        return super().get(db, profile_id)

    @classmethod
    def update(cls, db: Session, profile_id: str, payload: SlaProfileUpdate):
        return super().update(db, profile_id, payload)

    @classmethod
    def delete(cls, db: Session, profile_id: str):
        return super().delete(db, profile_id)
