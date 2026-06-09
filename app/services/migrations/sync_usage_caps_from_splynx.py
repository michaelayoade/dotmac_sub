"""Import per-plan data caps from Splynx into UsageAllowances.

Splynx keeps each internet tariff's monthly data cap in ``fup_limits``
(``traffic_amount`` in bytes; 0 + ``bonus_is_unlimited`` = uncapped). This maps a
capped tariff onto a ``UsageAllowance`` (``included_gb``) and links it to the
corresponding ``CatalogOffer`` via ``splynx_tariff_id`` — which is what turns the
already-built metering (used_gb) and FUP machinery on for that plan.

Idempotent: one allowance per offer (keyed by ``offer.usage_allowance_id``); a
re-run updates it in place. Uncapped tariffs are left as unlimited.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import CatalogOffer, UsageAllowance

logger = logging.getLogger(__name__)

_GIB = 1024**3
# Splynx speeds are kbps with 1 Mbps = 1024 kbps (binary).
_KBPS_PER_MBPS = 1024


def _throttle_mbps(row: dict) -> int | None:
    """Throttle speed (whole Mbps) after the cap, if the tariff throttles
    (action=decrease with a fixed downstream); a 'block' action cuts off
    instead, so None."""
    if str(row.get("action") or "").lower() != "decrease":
        return None
    fixed_down = int(row.get("fixed_down") or 0)
    if fixed_down <= 0:
        return None
    return max(1, round(fixed_down / _KBPS_PER_MBPS))


def import_usage_caps(
    db: Session, fup_limits_rows: list[dict], *, commit: bool = True
) -> dict:
    """Create/refresh UsageAllowances from capped Splynx fup_limits rows and link
    them to offers. Returns counts."""
    summary = {"capped": 0, "uncapped_skipped": 0, "no_offer": 0}
    for row in fup_limits_rows:
        amount = int(row.get("traffic_amount") or 0)
        if amount <= 0:
            summary["uncapped_skipped"] += 1
            continue
        tariff_id = row.get("tariff_id")
        offer = db.scalars(
            select(CatalogOffer).where(CatalogOffer.splynx_tariff_id == tariff_id)
        ).first()
        if offer is None:
            summary["no_offer"] += 1
            continue

        included_gb = max(1, round(amount / _GIB))
        allowance = (
            db.get(UsageAllowance, offer.usage_allowance_id)
            if offer.usage_allowance_id
            else None
        )
        if allowance is None:
            allowance = UsageAllowance(name=offer.name[:120])
            db.add(allowance)
            db.flush()
            offer.usage_allowance_id = allowance.id
        allowance.name = offer.name[:120]
        allowance.included_gb = included_gb
        allowance.throttle_rate_mbps = _throttle_mbps(row)
        allowance.is_active = True
        summary["capped"] += 1

    if commit:
        db.commit()
    else:
        db.flush()
    logger.info("splynx_usage_cap_import_complete", extra={"summary": summary})
    return summary


def sync_usage_caps_from_splynx(db: Session, *, commit: bool = True) -> dict:
    """Fetch Splynx fup_limits and import the caps. Requires the Splynx DB env."""
    from app.services.migrations.db_connections import fetch_all, splynx_connection

    with splynx_connection() as conn:
        rows = fetch_all(conn, "SELECT * FROM fup_limits")
    return import_usage_caps(db, list(rows), commit=commit)
