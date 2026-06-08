"""Import add-ons from Splynx into the catalog.

Two Splynx sources map to add-ons:

- ``tariffs_one_time``  → one-time add-ons (installation, relocation, support
  call-out, device replacement, …), priced ``one_time``.
- ``tariffs_custom``    → only the public-IP-block entries (titles like
  ``/29 IP``); priced ``recurring`` and flagged ``ip_is_public`` with the prefix
  size. The plan-shaped custom tariffs are already imported as offers, so they
  are skipped here.

Idempotent: every add-on records ``splynx_source`` (``custom:<id>`` /
``one_time:<id>``); a re-run updates the existing row instead of duplicating.
Nothing existing is modified — this only adds/refreshes add-on catalog rows.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    PriceType,
)

logger = logging.getLogger(__name__)

_IP_TITLE = re.compile(r"^\s*/(\d{1,2})\s*IP\s*$", re.IGNORECASE)
_DEFAULT_CURRENCY = "NGN"


def ip_prefix_length(title: str) -> int | None:
    """Return the prefix size for an IP-block title (``/29 IP`` → 29), else None."""
    match = _IP_TITLE.match(title or "")
    if not match:
        return None
    prefix = int(match.group(1))
    return prefix if 0 < prefix <= 32 else None


def _one_time_type(title: str) -> AddOnType:
    t = (title or "").lower()
    if any(w in t for w in ("install", "relocat", "dropcable", "rerun")):
        return AddOnType.install_fee
    if "support" in t:
        return AddOnType.premium_support
    return AddOnType.custom


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _truthy(value: object) -> bool:
    return str(value).strip() not in ("", "0", "None", "False", "false")


def _upsert_addon(
    db: Session,
    *,
    source: str,
    name: str,
    addon_type: AddOnType,
    description: str | None,
    ip_is_public: bool = False,
    ip_prefix: int | None = None,
) -> AddOn:
    add_on = db.scalars(select(AddOn).where(AddOn.splynx_source == source)).first()
    if add_on is None:
        add_on = AddOn(splynx_source=source)
        db.add(add_on)
    add_on.name = name[:120]
    add_on.addon_type = addon_type
    add_on.description = description or None
    add_on.is_active = True
    add_on.ip_is_public = ip_is_public
    add_on.ip_prefix_length = ip_prefix
    return add_on


def _set_single_price(
    db: Session,
    add_on: AddOn,
    *,
    amount: Decimal,
    currency: str,
    price_type: PriceType,
) -> None:
    """Make the given price the add-on's only active price (deactivate others)."""
    db.flush()  # ensure add_on.id
    existing = db.scalars(
        select(AddOnPrice).where(AddOnPrice.add_on_id == add_on.id)
    ).all()
    matched = None
    for price in existing:
        if price.price_type == price_type and (price.currency or _DEFAULT_CURRENCY) == (
            currency or _DEFAULT_CURRENCY
        ):
            matched = price
            price.amount = amount
            price.is_active = True
        else:
            price.is_active = False
    if matched is None:
        db.add(
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=price_type,
                amount=amount,
                currency=currency or _DEFAULT_CURRENCY,
                billing_cycle=(
                    BillingCycle.monthly if price_type == PriceType.recurring else None
                ),
                is_active=True,
            )
        )


def import_addon_rows(
    db: Session,
    one_time_rows: list[dict],
    custom_rows: list[dict],
    *,
    commit: bool = True,
) -> dict:
    """Core import (pure of any Splynx connection). Returns counts. With
    ``commit=False`` the work is only flushed, so a caller can inspect then roll
    back (dry-run)."""
    summary = {"one_time": 0, "ip_blocks": 0, "skipped": 0}

    for row in one_time_rows:
        if _truthy(row.get("deleted")) or (
            "enabled" in row and not _truthy(row.get("enabled"))
        ):
            summary["skipped"] += 1
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            summary["skipped"] += 1
            continue
        add_on = _upsert_addon(
            db,
            source=f"one_time:{row['id']}",
            name=title,
            addon_type=_one_time_type(title),
            description=str(row.get("service_description") or "") or None,
        )
        _set_single_price(
            db,
            add_on,
            amount=_to_decimal(row.get("price")),
            currency=_DEFAULT_CURRENCY,
            price_type=PriceType.one_time,
        )
        summary["one_time"] += 1

    for row in custom_rows:
        if _truthy(row.get("deleted")):
            summary["skipped"] += 1
            continue
        title = str(row.get("title") or "").strip()
        prefix = ip_prefix_length(title)
        if prefix is None:
            # Plan-shaped custom tariffs are imported as offers, not add-ons.
            summary["skipped"] += 1
            continue
        add_on = _upsert_addon(
            db,
            source=f"custom:{row['id']}",
            name=title,
            addon_type=(AddOnType.static_ip if prefix == 32 else AddOnType.extra_ip),
            description=f"Public IPv4 /{prefix} block.",
            ip_is_public=True,
            ip_prefix=prefix,
        )
        _set_single_price(
            db,
            add_on,
            amount=_to_decimal(row.get("price")),
            currency=_DEFAULT_CURRENCY,
            price_type=PriceType.recurring,
        )
        summary["ip_blocks"] += 1

    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("splynx_addon_import_complete", extra={"summary": summary})
    return summary


def sync_addons_from_splynx(db: Session) -> dict:
    """Fetch the Splynx add-on sources and import them. Requires Splynx DB env."""
    from app.services.migrations.db_connections import fetch_all, splynx_connection

    with splynx_connection() as conn:
        one_time = fetch_all(conn, "SELECT * FROM tariffs_one_time")
        custom = fetch_all(conn, "SELECT * FROM tariffs_custom")
    return import_addon_rows(db, list(one_time), list(custom))
