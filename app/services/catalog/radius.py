"""RADIUS profile and attribute management services."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import (
    CatalogOffer,
    NasVendor,
    OfferRadiusProfile,
    RadiusAttribute,
    RadiusProfile,
)
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    OfferRadiusProfileCreate,
    OfferRadiusProfileUpdate,
    RadiusAttributeCreate,
    RadiusAttributeUpdate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state, apply_optional_equals


class RadiusProfiles(CRUDManager[RadiusProfile]):
    model = RadiusProfile
    not_found_detail = "RADIUS profile not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

    @staticmethod
    def create(db: Session, payload: RadiusProfileCreate):
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
        profile = RadiusProfile(**data)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return profile

    @classmethod
    def get(cls, db: Session, profile_id: str):
        return super().get(db, profile_id)

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
        query = db.query(RadiusProfile)
        if vendor:
            query = query.filter(
                RadiusProfile.vendor == validate_enum(vendor, NasVendor, "vendor")
            )
        query = apply_active_state(query, RadiusProfile.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusProfile.created_at, "name": RadiusProfile.name},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, profile_id: str, payload: RadiusProfileUpdate):
        return super().update(db, profile_id, payload)

    @classmethod
    def delete(cls, db: Session, profile_id: str):
        return super().delete(db, profile_id)


class RadiusAttributes(CRUDManager[RadiusAttribute]):
    model = RadiusAttribute
    not_found_detail = "RADIUS attribute not found"

    @staticmethod
    def create(db: Session, payload: RadiusAttributeCreate):
        profile = db.get(RadiusProfile, payload.profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        attribute = RadiusAttribute(**payload.model_dump())
        db.add(attribute)
        db.commit()
        db.refresh(attribute)
        return attribute

    @classmethod
    def get(cls, db: Session, attribute_id: str):
        return super().get(db, attribute_id)

    @staticmethod
    def list(
        db: Session,
        profile_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(RadiusAttribute)
        query = apply_optional_equals(query, {RadiusAttribute.profile_id: profile_id})
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"attribute": RadiusAttribute.attribute},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, attribute_id: str, payload: RadiusAttributeUpdate):
        attribute = db.get(RadiusAttribute, attribute_id)
        if not attribute:
            raise HTTPException(status_code=404, detail="RADIUS attribute not found")
        data = payload.model_dump(exclude_unset=True)
        if "profile_id" in data:
            profile = db.get(RadiusProfile, data["profile_id"])
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
        for key, value in data.items():
            setattr(attribute, key, value)
        db.commit()
        db.refresh(attribute)
        return attribute

    @classmethod
    def delete(cls, db: Session, attribute_id: str):
        return super().delete(db, attribute_id)


class OfferRadiusProfiles(CRUDManager[OfferRadiusProfile]):
    model = OfferRadiusProfile
    not_found_detail = "Offer RADIUS profile link not found"

    @staticmethod
    def create(db: Session, payload: OfferRadiusProfileCreate):
        offer = db.get(CatalogOffer, payload.offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        profile = db.get(RadiusProfile, payload.profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        link = OfferRadiusProfile(**payload.model_dump())
        db.add(link)
        db.commit()
        db.refresh(link)
        return link

    @classmethod
    def get(cls, db: Session, link_id: str):
        return super().get(db, link_id)

    @staticmethod
    def list(
        db: Session,
        offer_id: str | None,
        profile_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferRadiusProfile)
        query = apply_optional_equals(
            query,
            {
                OfferRadiusProfile.offer_id: offer_id,
                OfferRadiusProfile.profile_id: profile_id,
            },
        )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"offer_id": OfferRadiusProfile.offer_id},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, link_id: str, payload: OfferRadiusProfileUpdate):
        link = db.get(OfferRadiusProfile, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Offer RADIUS profile link not found")
        data = payload.model_dump(exclude_unset=True)
        if "offer_id" in data:
            offer = db.get(CatalogOffer, data["offer_id"])
            if not offer:
                raise HTTPException(status_code=404, detail="Offer not found")
        if "profile_id" in data:
            profile = db.get(RadiusProfile, data["profile_id"])
            if not profile:
                raise HTTPException(status_code=404, detail="RADIUS profile not found")
        for key, value in data.items():
            setattr(link, key, value)
        db.commit()
        db.refresh(link)
        return link

    @classmethod
    def delete(cls, db: Session, link_id: str):
        return super().delete(db, link_id)
