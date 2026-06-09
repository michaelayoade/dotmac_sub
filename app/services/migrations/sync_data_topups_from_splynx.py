"""Import data top-up products from Splynx ``cap_tariff`` into add-ons.

Splynx sells extra-data bundles ("10GB" ₦3000, "250GB Top up" ₦30000) per
tariff in ``cap_tariff``. Each maps to a data-top-up ``AddOn`` (``grant_gb`` =
the GB it credits on purchase) priced ``one_time``, linked to the tariff's offer
via ``OfferAddOn`` so the plan's customers can buy it.

Idempotent on ``splynx_source`` = ``cap_tariff:<id>``.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    AddOnPrice,
    AddOnType,
    CatalogOffer,
    OfferAddOn,
    PriceType,
)

logger = logging.getLogger(__name__)


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _validity_days(row: dict) -> int | None:
    """Splynx cap_tariff.validity -> top-up validity in days. 'end_of_period'
    (and blank) -> None (expires at period end); a numeric value is a count of
    billing periods (~30 days each)."""
    raw = str(row.get("validity") or "").strip().lower()
    if not raw or raw == "end_of_period":
        return None
    try:
        periods = int(raw)
    except ValueError:
        return None
    return periods * 30 if periods > 0 else None


def import_data_topups(
    db: Session, cap_tariff_rows: list[dict], *, commit: bool = True
) -> dict:
    summary = {"topups": 0, "skipped": 0, "no_offer": 0}
    for row in cap_tariff_rows:
        if str(row.get("deleted") or "").strip() == "1":
            summary["skipped"] += 1
            continue
        if str(row.get("amount_in") or "").lower() != "gb":
            summary["skipped"] += 1
            continue
        gb = int(row.get("amount") or 0)
        title = str(row.get("title") or "").strip()
        if gb <= 0 or not title:
            summary["skipped"] += 1
            continue
        offer = db.scalars(
            select(CatalogOffer).where(
                CatalogOffer.splynx_tariff_id == row.get("tariff_id")
            )
        ).first()
        if offer is None:
            summary["no_offer"] += 1
            continue

        source = f"cap_tariff:{row['id']}"
        add_on = db.scalars(select(AddOn).where(AddOn.splynx_source == source)).first()
        if add_on is None:
            add_on = AddOn(splynx_source=source)
            db.add(add_on)
        add_on.name = title[:120]
        add_on.addon_type = AddOnType.custom
        add_on.description = f"{gb} GB data top-up"
        add_on.is_active = True
        add_on.grant_gb = gb
        add_on.validity_days = _validity_days(row)
        db.flush()

        # one-time price (deactivate any others)
        prices = db.scalars(
            select(AddOnPrice).where(AddOnPrice.add_on_id == add_on.id)
        ).all()
        matched = None
        for p in prices:
            if p.price_type == PriceType.one_time:
                matched = p
                p.amount = _to_decimal(row.get("price"))
                p.is_active = True
            else:
                p.is_active = False
        if matched is None:
            db.add(
                AddOnPrice(
                    add_on_id=add_on.id,
                    price_type=PriceType.one_time,
                    amount=_to_decimal(row.get("price")),
                    currency="NGN",
                    is_active=True,
                )
            )

        # link to the plan (idempotent)
        link = db.scalars(
            select(OfferAddOn).where(
                OfferAddOn.offer_id == offer.id,
                OfferAddOn.add_on_id == add_on.id,
            )
        ).first()
        if link is None:
            db.add(
                OfferAddOn(
                    offer_id=offer.id,
                    add_on_id=add_on.id,
                    is_required=False,
                    min_quantity=1,
                )
            )
        summary["topups"] += 1

    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("splynx_data_topup_import_complete", extra={"summary": summary})
    return summary


def sync_data_topups_from_splynx(db: Session, *, commit: bool = True) -> dict:
    """Fetch Splynx cap_tariff and import the top-ups. Requires the Splynx env."""
    from app.services.migrations.db_connections import fetch_all, splynx_connection

    with splynx_connection() as conn:
        rows = fetch_all(conn, "SELECT * FROM cap_tariff")
    return import_data_topups(db, list(rows), commit=commit)
