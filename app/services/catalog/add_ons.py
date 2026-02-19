"""Add-on management services.

Provides services for AddOns, AddOnPrices, and OfferAddOns.
"""

from sqlalchemy.orm import Session

from app.models.catalog import AddOn, AddOnPrice, AddOnType, PriceType
from app.models.domain_settings import SettingDomain
from app.schemas.catalog import (
    AddOnCreate,
    AddOnPriceCreate,
    AddOnPriceUpdate,
    AddOnUpdate,
)
from app.services import settings_spec
from app.services.common import apply_ordering, apply_pagination, validate_enum
from app.services.crud import CRUDManager
from app.services.query_builders import apply_active_state, apply_optional_equals


class AddOns(CRUDManager[AddOn]):
    model = AddOn
    not_found_detail = "Add-on not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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

    @classmethod
    def get(cls, db: Session, add_on_id: str):
        return super().get(db, add_on_id)

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
        query = apply_active_state(query, AddOn.is_active, is_active)
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

    @classmethod
    def update(cls, db: Session, add_on_id: str, payload: AddOnUpdate):
        return super().update(db, add_on_id, payload)

    @classmethod
    def delete(cls, db: Session, add_on_id: str):
        return super().delete(db, add_on_id)


class AddOnPrices(CRUDManager[AddOnPrice]):
    model = AddOnPrice
    not_found_detail = "Add-on price not found"
    soft_delete_field = "is_active"
    soft_delete_value = False

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

    @classmethod
    def get(cls, db: Session, price_id: str):
        return super().get(db, price_id)

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
        query = apply_optional_equals(query, {AddOnPrice.add_on_id: add_on_id})
        query = apply_active_state(query, AddOnPrice.is_active, is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": AddOnPrice.created_at, "amount": AddOnPrice.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @classmethod
    def update(cls, db: Session, price_id: str, payload: AddOnPriceUpdate):
        return super().update(db, price_id, payload)

    @classmethod
    def delete(cls, db: Session, price_id: str):
        return super().delete(db, price_id)
