"""Service helpers for admin catalog pricing calculator routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from app.models.billing import TaxApplication
from app.models.catalog import BillingCycle
from app.services import catalog as catalog_service
from app.services.billing._common import _calculate_tax_amount
from app.services.catalog.subscriptions import _add_months
from app.services.common import round_money, to_decimal

logger = logging.getLogger(__name__)


def _cycle_bounds(start: datetime, cycle: BillingCycle) -> tuple[datetime, datetime]:
    """Calendar boundaries ``[cycle_start, next_billing)`` of the billing cycle
    that contains ``start``.

    This mirrors the cycle geometry used by real invoicing
    (``catalog.subscriptions._billing_cycle_start`` /
    ``billing_automation._prorated_amount``) so the calculator's first-bill
    proration lines up with what would actually be charged for a mid-cycle
    activation.
    """
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if cycle == BillingCycle.daily:
        return day_start, day_start + timedelta(days=1)
    if cycle == BillingCycle.weekly:
        week_start = day_start - timedelta(days=day_start.weekday())
        return week_start, week_start + timedelta(days=7)
    if cycle == BillingCycle.quarterly:
        quarter_month = ((start.month - 1) // 3) * 3 + 1
        prev = day_start.replace(month=quarter_month, day=1)
        return prev, _add_months(prev, 3)
    if cycle == BillingCycle.annual:
        prev = day_start.replace(month=1, day=1)
        return prev, _add_months(prev, 12)
    # monthly (default)
    prev = day_start.replace(day=1)
    return prev, _add_months(prev, 1)


def first_bill_proration_ratio(start: datetime, cycle: BillingCycle) -> Decimal:
    """Fraction of the first billing cycle a mid-cycle ``start`` consumes.

    Equal to ``(next_billing - start) / (next_billing - cycle_start)`` — the same
    ``usage / period`` ratio invoicing uses in
    ``billing_automation._prorated_amount``. A start on a cycle boundary (or no
    partial period) yields ``1`` so the first bill equals a full period.
    """
    cycle_start, next_billing = _cycle_bounds(start, cycle)
    total = (next_billing - cycle_start).total_seconds()
    remaining = (next_billing - start).total_seconds()
    if total <= 0:
        return Decimal("1")
    ratio = to_decimal(remaining) / to_decimal(total)
    if ratio < Decimal("0"):
        return Decimal("0")
    if ratio > Decimal("1"):
        return Decimal("1")
    return ratio


def _coerce_cycle(billing_cycle: BillingCycle | str | None) -> BillingCycle | None:
    if billing_cycle is None or isinstance(billing_cycle, BillingCycle):
        return billing_cycle
    try:
        return BillingCycle(str(billing_cycle))
    except ValueError:
        return None


def compute_monthly(
    *,
    recurring_subtotal,
    overage_charge,
    with_vat: bool,
    vat_percent,
) -> dict[str, Decimal]:
    """Steady-state monthly total: recurring + overage, plus exclusive VAT.

    VAT is applied on top (exclusive) via the shared invoicing helper
    ``billing._common._calculate_tax_amount``, matching the default
    ``billing.default_tax_application`` of ``exclusive``.
    """
    subtotal = round_money(to_decimal(recurring_subtotal) + to_decimal(overage_charge))
    application = TaxApplication.exclusive if with_vat else TaxApplication.exempt
    vat = _calculate_tax_amount(subtotal, to_decimal(vat_percent), application)
    return {
        "subtotal": subtotal,
        "vat_amount": vat,
        "total": round_money(subtotal + vat),
    }


def compute_first_bill(
    *,
    recurring_subtotal,
    one_time_total,
    overage_charge,
    with_vat: bool,
    vat_percent,
    start: datetime | None = None,
    billing_cycle: BillingCycle | str | None = None,
) -> dict[str, Decimal]:
    """First-bill total for a new subscription.

    VAT applies to the *whole* taxable base — the (prorated) recurring charge,
    overage, and any one-time fees — because invoicing tags every line of a
    taxable subscription with the same tax rate (see
    ``billing_automation._resolve_offer_tax_rate_id`` and
    ``_add_recurring_addon_lines``); one-time fees are never VAT-free. When a
    ``start`` date lands mid-cycle the recurring portion is prorated
    (``first_bill_proration_ratio``); one-time fees are never prorated. With no
    partial start the ratio is ``1``, so the result equals a full-period bill.
    """
    recurring_subtotal = round_money(recurring_subtotal)
    one_time_total = round_money(one_time_total)
    overage_charge = round_money(overage_charge)

    ratio = Decimal("1")
    cycle = _coerce_cycle(billing_cycle)
    if start is not None and cycle is not None:
        ratio = first_bill_proration_ratio(start, cycle)

    prorated_recurring = round_money(recurring_subtotal * ratio)
    taxable_base = round_money(prorated_recurring + overage_charge + one_time_total)
    application = TaxApplication.exclusive if with_vat else TaxApplication.exempt
    vat = _calculate_tax_amount(taxable_base, to_decimal(vat_percent), application)
    return {
        "proration_ratio": ratio,
        "prorated_recurring": prorated_recurring,
        "taxable_base": taxable_base,
        "vat_amount": vat,
        "total": round_money(taxable_base + vat),
    }


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
                "billing_cycle": offer.billing_cycle.value
                if offer.billing_cycle
                else "",
                "usage_allowance_id": str(offer.usage_allowance_id)
                if offer.usage_allowance_id
                else "",
                "with_vat": offer.with_vat,
                "vat_percent": float(offer.vat_percent) if offer.vat_percent else 0,
                "prices": [
                    {
                        "price_type": p.price_type.value if p.price_type else "",
                        "amount": float(p.amount) if p.amount else 0,
                        "currency": p.currency or "NGN",
                        "billing_cycle": p.billing_cycle.value
                        if p.billing_cycle
                        else "",
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
                        "billing_cycle": p.billing_cycle.value
                        if p.billing_cycle
                        else "",
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
