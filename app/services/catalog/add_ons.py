"""Add-on management services.

Provides services for AddOns, AddOnPrices, and OfferAddOns.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import AddOn, AddOnPrice, AddOnType, PriceType
from app.models.domain_settings import SettingDomain
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.response import ListResponseMixin
from app.services import settings_spec
from app.schemas.catalog import (
    AddOnCreate,
    AddOnPriceCreate,
    AddOnPriceUpdate,
    AddOnUpdate,
)


class AddOns(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AddOnCreate):
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "addon_type" not in fields_set:
            default_addon_type = settings_spec.resolve_value(
                db, SettingDomain.catalog, "default_addon_type"
            )
            if default_addon_type:
                data["addon_type"] = validate_enum(
                    default_addon_type, AddOnType, "addon_type"
                )
        add_on = AddOn(**data)
        db.add(add_on)
        db.commit()
        db.refresh(add_on)
        return add_on

    @staticmethod
    def get(db: Session, add_on_id: str):
        add_on = db.get(AddOn, add_on_id)
        if not add_on:
            raise HTTPException(status_code=404, detail="Add-on not found")
        return add_on

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        addon_type: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AddOn)
        if is_active is None:
            query = query.filter(AddOn.is_active.is_(True))
        else:
            query = query.filter(AddOn.is_active == is_active)
        if addon_type:
            query = query.filter(
                AddOn.addon_type == validate_enum(addon_type, AddOnType, "addon_type")
            )
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AddOn.created_at, "name": AddOn.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, add_on_id: str, payload: AddOnUpdate):
        add_on = db.get(AddOn, add_on_id)
        if not add_on:
            raise HTTPException(status_code=404, detail="Add-on not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(add_on, key, value)
        db.commit()
        db.refresh(add_on)
        return add_on

    @staticmethod
    def delete(db: Session, add_on_id: str):
        add_on = db.get(AddOn, add_on_id)
        if not add_on:
            raise HTTPException(status_code=404, detail="Add-on not found")
        add_on.is_active = False
        db.commit()


class AddOnPrices(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: AddOnPriceCreate):
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
        price = AddOnPrice(**data)
        db.add(price)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def get(db: Session, price_id: str):
        price = db.get(AddOnPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Add-on price not found")
        return price

    @staticmethod
    def list(
        db: Session,
        add_on_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(AddOnPrice)
        if add_on_id:
            query = query.filter(AddOnPrice.add_on_id == add_on_id)
        if is_active is None:
            query = query.filter(AddOnPrice.is_active.is_(True))
        else:
            query = query.filter(AddOnPrice.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AddOnPrice.created_at, "amount": AddOnPrice.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, price_id: str, payload: AddOnPriceUpdate):
        price = db.get(AddOnPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Add-on price not found")
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(price, key, value)
        db.commit()
        db.refresh(price)
        return price

    @staticmethod
    def delete(db: Session, price_id: str):
        price = db.get(AddOnPrice, price_id)
        if not price:
            raise HTTPException(status_code=404, detail="Add-on price not found")
        price.is_active = False
        db.commit()
