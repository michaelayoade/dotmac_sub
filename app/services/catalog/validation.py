"""Offer validation service."""

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOnPrice,
    CatalogOffer,
    OfferAddOn,
    OfferPrice,
    OfferStatus,
    OfferVersionPrice,
    PriceType,
)
from app.services.response import ListResponseMixin
from app.validators import catalog as catalog_validators
from app.schemas.catalog import (
    OfferValidationPrice,
    OfferValidationRequest,
    OfferValidationResponse,
)


class OfferValidation(ListResponseMixin):
    @staticmethod
    def validate(db: Session, payload: OfferValidationRequest) -> OfferValidationResponse:
        errors: list[str] = []
        offer = db.get(CatalogOffer, payload.offer_id)
        if not offer:
            raise HTTPException(status_code=404, detail="Offer not found")
        if not offer.is_active or offer.status != OfferStatus.active:
            errors.append("Offer is not active")
        billing_cycle = payload.billing_cycle or offer.billing_cycle
        if payload.offer_version_id:
            try:
                catalog_validators.validate_offer_version_active(
                    db,
                    str(payload.offer_version_id),
                    str(payload.offer_id),
                    datetime.now(timezone.utc),
                )
            except HTTPException as exc:
                errors.append(str(exc.detail))

        add_on_links = (
            db.query(OfferAddOn)
            .filter(OfferAddOn.offer_id == payload.offer_id)
            .all()
        )
        required_add_on_ids = {link.add_on_id for link in add_on_links if link.is_required}
        provided_add_on_ids = {item.add_on_id for item in payload.add_ons}
        missing_required = required_add_on_ids.difference(provided_add_on_ids)
        if missing_required:
            errors.append("Missing required add-ons")

        prices: list[OfferValidationPrice] = []
        recurring_total = Decimal("0.00")
        one_time_total = Decimal("0.00")
        usage_total = Decimal("0.00")

        offer_prices: Sequence[OfferPrice | OfferVersionPrice]
        if payload.offer_version_id:
            offer_prices = (
                db.query(OfferVersionPrice)
                .filter(OfferVersionPrice.offer_version_id == payload.offer_version_id)
                .filter(OfferVersionPrice.is_active.is_(True))
                .all()
            )
            source = "offer_version"
        else:
            offer_prices = (
                db.query(OfferPrice)
                .filter(OfferPrice.offer_id == payload.offer_id)
                .filter(OfferPrice.is_active.is_(True))
                .all()
            )
            source = "offer"

        for offer_price in offer_prices:
            if offer_price.billing_cycle and offer_price.billing_cycle != billing_cycle:
                continue
            extended = Decimal(str(offer_price.amount))
            prices.append(
                OfferValidationPrice(
                    source=source,
                    price_type=offer_price.price_type,
                    amount=offer_price.amount,
                    currency=offer_price.currency,
                    billing_cycle=offer_price.billing_cycle,
                    unit=offer_price.unit,
                    description=offer_price.description,
                    extended_amount=extended,
                )
            )
            if offer_price.price_type == PriceType.recurring:
                recurring_total += extended
            elif offer_price.price_type == PriceType.one_time:
                one_time_total += extended
            else:
                usage_total += extended

        for add_on in payload.add_ons:
            try:
                catalog_validators.validate_offer_add_on(
                    db, str(payload.offer_id), str(add_on.add_on_id), add_on.quantity
                )
            except HTTPException as exc:
                errors.append(str(exc.detail))
                continue
            add_on_prices = (
                db.query(AddOnPrice)
                .filter(AddOnPrice.add_on_id == add_on.add_on_id)
                .filter(AddOnPrice.is_active.is_(True))
                .all()
            )
            for add_on_price in add_on_prices:
                if add_on_price.billing_cycle and add_on_price.billing_cycle != billing_cycle:
                    continue
                extended = Decimal(str(add_on_price.amount)) * Decimal(add_on.quantity)
                prices.append(
                    OfferValidationPrice(
                        source="add_on",
                        price_type=add_on_price.price_type,
                        amount=add_on_price.amount,
                        currency=add_on_price.currency,
                        billing_cycle=add_on_price.billing_cycle,
                        unit=add_on_price.unit,
                        description=add_on_price.description,
                        add_on_id=add_on.add_on_id,
                        quantity=add_on.quantity,
                        extended_amount=extended,
                    )
                )
                if add_on_price.price_type == PriceType.recurring:
                    recurring_total += extended
                elif add_on_price.price_type == PriceType.one_time:
                    one_time_total += extended
                else:
                    usage_total += extended

        return OfferValidationResponse(
            valid=len(errors) == 0,
            errors=errors,
            offer_id=payload.offer_id,
            offer_version_id=payload.offer_version_id,
            billing_cycle=billing_cycle,
            prices=prices,
            recurring_total=recurring_total,
            one_time_total=one_time_total,
            usage_total=usage_total,
        )
