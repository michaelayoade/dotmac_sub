"""Service helpers for admin catalog pricing calculator routes."""

from __future__ import annotations

from app.services import catalog as catalog_service


def calculator_page_data(db) -> dict[str, object]:
    """Build payload for pricing calculator page."""
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status="active",
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    add_ons = catalog_service.add_ons.list(
        db=db,
        is_active=True,
        addon_type=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )
    usage_allowances = catalog_service.usage_allowances.list(
        db=db,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    offers_with_prices = []
    for offer in offers:
        prices = catalog_service.offer_prices.list(
            db=db,
            offer_id=str(offer.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        offers_with_prices.append(
            {
                "id": str(offer.id),
                "name": offer.name,
                "code": offer.code or "",
                "service_type": offer.service_type.value if offer.service_type else "",
                "billing_cycle": offer.billing_cycle.value if offer.billing_cycle else "",
                "usage_allowance_id": str(offer.usage_allowance_id) if offer.usage_allowance_id else "",
                "with_vat": offer.with_vat,
                "vat_percent": float(offer.vat_percent) if offer.vat_percent else 0,
                "prices": [
                    {
                        "price_type": p.price_type.value if p.price_type else "",
                        "amount": float(p.amount) if p.amount else 0,
                        "currency": p.currency or "NGN",
                        "billing_cycle": p.billing_cycle.value if p.billing_cycle else "",
                    }
                    for p in prices
                ],
            }
        )

    add_ons_with_prices = []
    for addon in add_ons:
        prices = catalog_service.add_on_prices.list(
            db=db,
            add_on_id=str(addon.id),
            is_active=True,
            order_by="created_at",
            order_dir="asc",
            limit=50,
            offset=0,
        )
        add_ons_with_prices.append(
            {
                "id": str(addon.id),
                "name": addon.name,
                "addon_type": addon.addon_type.value if addon.addon_type else "",
                "prices": [
                    {
                        "price_type": p.price_type.value if p.price_type else "",
                        "amount": float(p.amount) if p.amount else 0,
                        "currency": p.currency or "NGN",
                        "billing_cycle": p.billing_cycle.value if p.billing_cycle else "",
                    }
                    for p in prices
                ],
            }
        )

    usage_allowances_data = [
        {
            "id": str(ua.id),
            "name": ua.name,
            "included_gb": ua.included_gb or 0,
            "overage_rate": float(ua.overage_rate) if ua.overage_rate else 0,
            "overage_cap_gb": ua.overage_cap_gb or 0,
        }
        for ua in usage_allowances
    ]

    offer_addon_map = {}
    for offer in offers:
        offer_addons = catalog_service.offer_addons.list(
            db=db, offer_id=str(offer.id), limit=200
        )
        offer_addon_map[str(offer.id)] = [str(link.add_on_id) for link in offer_addons]

    return {
        "offers": offers_with_prices,
        "add_ons": add_ons_with_prices,
        "usage_allowances": usage_allowances_data,
        "offer_addon_map": offer_addon_map,
    }
