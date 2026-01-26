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
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.schemas.catalog import (
    OfferRadiusProfileCreate,
    OfferRadiusProfileUpdate,
    RadiusAttributeCreate,
    RadiusAttributeUpdate,
    RadiusProfileCreate,
    RadiusProfileUpdate,
)


class RadiusProfiles(ListResponseMixin):
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

    @staticmethod
    def get(db: Session, profile_id: str):
        profile = db.get(RadiusProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        return profile

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
        if is_active is None:
            query = query.filter(RadiusProfile.is_active.is_(True))
        else:
            query = query.filter(RadiusProfile.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": RadiusProfile.created_at, "name": RadiusProfile.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, profile_id: str, payload: RadiusProfileUpdate):
        profile = db.get(RadiusProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(profile, key, value)
        db.commit()
        db.refresh(profile)
        return profile

    @staticmethod
    def delete(db: Session, profile_id: str):
        profile = db.get(RadiusProfile, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="RADIUS profile not found")
        profile.is_active = False
        db.commit()


class RadiusAttributes(ListResponseMixin):
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

    @staticmethod
    def get(db: Session, attribute_id: str):
        attribute = db.get(RadiusAttribute, attribute_id)
        if not attribute:
            raise HTTPException(status_code=404, detail="RADIUS attribute not found")
        return attribute

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
        if profile_id:
            query = query.filter(RadiusAttribute.profile_id == profile_id)
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

    @staticmethod
    def delete(db: Session, attribute_id: str):
        attribute = db.get(RadiusAttribute, attribute_id)
        if not attribute:
            raise HTTPException(status_code=404, detail="RADIUS attribute not found")
        db.delete(attribute)
        db.commit()


class OfferRadiusProfiles(ListResponseMixin):
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

    @staticmethod
    def get(db: Session, link_id: str):
        link = db.get(OfferRadiusProfile, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Offer RADIUS profile link not found")
        return link

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
        if offer_id:
            query = query.filter(OfferRadiusProfile.offer_id == offer_id)
        if profile_id:
            query = query.filter(OfferRadiusProfile.profile_id == profile_id)
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

    @staticmethod
    def delete(db: Session, link_id: str):
        link = db.get(OfferRadiusProfile, link_id)
        if not link:
            raise HTTPException(status_code=404, detail="Offer RADIUS profile link not found")
        db.delete(link)
        db.commit()
