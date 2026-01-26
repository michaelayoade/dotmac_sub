"""Offer management services.

Provides services for Offers, OfferPrices, OfferVersions, and OfferVersionPrices.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    ContractTerm,
    OfferAddOn,
    OfferPrice,
    OfferStatus,
    OfferVersion,
    OfferVersionPrice,
    PriceType,
    ServiceType,
)
from app.models.domain_settings import SettingDomain
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.schemas.catalog import (
    CatalogOfferCreate,
    CatalogOfferUpdate,
    OfferPriceCreate,
    OfferPriceUpdate,
    OfferVersionCreate,
    OfferVersionPriceCreate,
    OfferVersionPriceUpdate,
    OfferVersionUpdate,
)


class Offers(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CatalogOfferCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "billing_cycle" not in fields_set:
            default_billing_cycle = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_billing_cycle"
            )
            if default_billing_cycle:
                data["billing_cycle"] = validate_enum(
                    default_billing_cycle, BillingCycle, "billing_cycle"
                )
        if "billing_mode" not in fields_set:
            default_billing_mode = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_billing_mode"
            )
            if default_billing_mode:
                data["billing_mode"] = validate_enum(
                    default_billing_mode, BillingMode, "billing_mode"
                )
        if "contract_term" not in fields_set:
            default_contract_term = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_contract_term"
            )
            if default_contract_term:
                data["contract_term"] = validate_enum(
                    default_contract_term, ContractTerm, "contract_term"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_offer_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, OfferStatus, "status"
                )
        offer = CatalogOffer(**data)
        db.add(offer)
        db.commit()
        db.refresh(offer)
        return offer

    @staticmethod
    def get(db: Session, offer_id: str):
        offer = db.get(
            CatalogOffer,
            offer_id,
            options=[
                selectinload(CatalogOffer.region_zone),
                selectinload(CatalogOffer.usage_allowance),
                selectinload(CatalogOffer.sla_profile),
                selectinload(CatalogOffer.policy_set),
                selectinload(CatalogOffer.prices),
                selectinload(CatalogOffer.add_on_links).selectinload(OfferAddOn.add_on),
            ],
        )
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        return offer

    @staticmethod
    def list(
        db: Session,
        service_type: str | None,
        access_type: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CatalogOffer).options(
            selectinload(CatalogOffer.region_zone),
            selectinload(CatalogOffer.usage_allowance),
            selectinload(CatalogOffer.sla_profile),
            selectinload(CatalogOffer.policy_set),
            selectinload(CatalogOffer.prices),
            selectinload(CatalogOffer.add_on_links).selectinload(OfferAddOn.add_on),
        )
        if service_type:
            query = query.filter(
                CatalogOffer.service_type == validate_enum(service_type, ServiceType, "service_type")
            )
        if access_type:
            query = query.filter(
                CatalogOffer.access_type == validate_enum(access_type, AccessType, "access_type")
            )
        if status:
            query = query.filter(
                CatalogOffer.status == validate_enum(status, OfferStatus, "status")
            )
        if is_active is None:
            query = query.filter(CatalogOffer.is_active.is_(True))
        else:
            query = query.filter(CatalogOffer.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": CatalogOffer.created_at, "name": CatalogOffer.name, "status": CatalogOffer.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, offer_id: str, payload: CatalogOfferUpdate):
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(offer, key, value)
        db.commit()
        db.refresh(offer)
        return offer

    @staticmethod
    def delete(db: Session, offer_id: str):
        offer = db.get(CatalogOffer, offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        offer.status = OfferStatus.archived
        offer.is_active = False
        db.commit()


class OfferPrices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OfferPriceCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "price_type" not in fields_set:
            default_price_type = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_price_type"
            )
            if default_price_type:
                data["price_type"] = validate_enum(
                    default_price_type, PriceType, "price_type"
                )
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        price = OfferPrice(**data)
        db.add(price)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def get(db: Session, price_id: str):
        price = db.get(OfferPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer price not found")
        return price

    @staticmethod
    def list(
        db: Session,
        offer_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferPrice)
        if offer_id:
            query = query.filter(OfferPrice.offer_id == offer_id)
        if is_active is None:
            query = query.filter(OfferPrice.is_active.is_(True))
        else:
            query = query.filter(OfferPrice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OfferPrice.created_at, "amount": OfferPrice.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, price_id: str, payload: OfferPriceUpdate):
        price = db.get(OfferPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer price not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(price, key, value)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def delete(db: Session, price_id: str):
        price = db.get(OfferPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer price not found")
        price.is_active = False
        db.commit()


class OfferVersions(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OfferVersionCreate):
        offer = db.get(CatalogOffer, payload.offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "billing_cycle" not in fields_set:
            default_billing_cycle = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_billing_cycle"
            )
            if default_billing_cycle:
                data["billing_cycle"] = validate_enum(
                    default_billing_cycle, BillingCycle, "billing_cycle"
                )
        if "contract_term" not in fields_set:
            default_contract_term = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_contract_term"
            )
            if default_contract_term:
                data["contract_term"] = validate_enum(
                    default_contract_term, ContractTerm, "contract_term"
                )
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_offer_status"
            )
            if default_status:
                data["status"] = validate_enum(
                    default_status, OfferStatus, "status"
                )
        version = OfferVersion(**data)
        db.add(version)
        db.commit()
        db.refresh(version)
        return version

    @staticmethod
    def get(db: Session, version_id: str):
        version = db.get(OfferVersion, version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        return version

    @staticmethod
    def list(
        db: Session,
        offer_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferVersion)
        if offer_id:
            query = query.filter(OfferVersion.offer_id == offer_id)
        if is_active is None:
            query = query.filter(OfferVersion.is_active.is_(True))
        else:
            query = query.filter(OfferVersion.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OfferVersion.created_at, "version_number": OfferVersion.version_number},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, version_id: str, payload: OfferVersionUpdate):
        version = db.get(OfferVersion, version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        data = payload.model_dump(exclude_unset=True)
        if "offer_id" in data:
            offer = db.get(CatalogOffer, data["offer_id"])
            if not offer:
                raise HTTPException(status_code=404, detail="Offer not found")
        for key, value in data.items():
            setattr(version, key, value)
        db.commit()
        db.refresh(version)
        return version

    @staticmethod
    def delete(db: Session, version_id: str):
        version = db.get(OfferVersion, version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        version.is_active = False
        db.commit()


class OfferVersionPrices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: OfferVersionPriceCreate):
        version = db.get(OfferVersion, payload.offer_version_id)
        if not version:
            raise HTTPException(status_code=404, detail="Offer version not found")
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "price_type" not in fields_set:
            default_price_type = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_price_type"
            )
            if default_price_type:
                data["price_type"] = validate_enum(
                    default_price_type, PriceType, "price_type"
                )
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        price = OfferVersionPrice(**data)
        db.add(price)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def get(db: Session, price_id: str):
        price = db.get(OfferVersionPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer version price not found")
        return price

    @staticmethod
    def list(
        db: Session,
        offer_version_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(OfferVersionPrice)
        if offer_version_id:
            query = query.filter(OfferVersionPrice.offer_version_id == offer_version_id)
        if is_active is None:
            query = query.filter(OfferVersionPrice.is_active.is_(True))
        else:
            query = query.filter(OfferVersionPrice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": OfferVersionPrice.created_at, "amount": OfferVersionPrice.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, price_id: str, payload: OfferVersionPriceUpdate):
        price = db.get(OfferVersionPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer version price not found")
        data = payload.model_dump(exclude_unset=True)
        if "offer_version_id" in data:
            version = db.get(OfferVersion, data["offer_version_id"])
            if not version:
                raise HTTPException(status_code=404, detail="Offer version not found")
        for key, value in data.items():
            setattr(price, key, value)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def delete(db: Session, price_id: str):
        price = db.get(OfferVersionPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Offer version price not found")
        price.is_active = False
        db.commit()
