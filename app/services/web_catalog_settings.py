"""Service helpers for admin catalog settings web routes.

Provides paginated list+count queries, bulk deletes, CSV exports,
and nested-child sync logic so that routes remain thin wrappers.
"""

from __future__ import annotations

import csv
import logging
from io import StringIO
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    BillingCycle,
    DunningAction,
    PolicySet,
    PriceType,
    PriceUnit,
    RegionZone,
    SlaProfile,
    UsageAllowance,
)
from app.schemas.catalog import (
    AddOnPriceCreate,
    AddOnPriceUpdate,
    PolicyDunningStepCreate,
    PolicyDunningStepUpdate,
)
from app.services import catalog as catalog_service
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paginated list result type
# ---------------------------------------------------------------------------

class PaginatedResult:
    """Holds items, total count, and total pages for a paginated query."""

    __slots__ = ("items", "total", "total_pages")

    def __init__(self, items: list[Any], total: int, total_pages: int) -> None:
        self.items = items
        self.total = total
        self.total_pages = total_pages


# ---------------------------------------------------------------------------
# Settings overview counts
# ---------------------------------------------------------------------------


def settings_overview_counts(db: Session) -> dict[str, int]:
    """Return total and active counts for each catalog-settings entity type."""
    counts: dict[str, int] = {}
    for model, prefix in [
        (RegionZone, "region_zones"),
        (UsageAllowance, "usage_allowances"),
        (SlaProfile, "sla_profiles"),
        (PolicySet, "policy_sets"),
        (AddOn, "add_ons"),
    ]:
        # mypy doesn't understand `model` is one of the explicit ORM classes above.
        model_id = model.id  # type: ignore[attr-defined]
        model_is_active = model.is_active  # type: ignore[attr-defined]
        total = db.execute(select(func.count(model_id))).scalar() or 0
        active = (
            db.execute(select(func.count(model_id)).where(model_is_active.is_(True))).scalar()
            or 0
        )
        counts[f"{prefix}_count"] = total
        counts[f"{prefix}_active"] = active
    return counts


# ---------------------------------------------------------------------------
# Paginated list helpers
# ---------------------------------------------------------------------------


def list_region_zones_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated region zones with optional status and search filters."""
    stmt = select(RegionZone)
    count_stmt = select(func.count(RegionZone.id))

    if is_active is not None:
        stmt = stmt.where(RegionZone.is_active == is_active)
        count_stmt = count_stmt.where(RegionZone.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = or_(
            RegionZone.name.ilike(term), RegionZone.code.ilike(term)
        )
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(RegionZone.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def list_usage_allowances_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated usage allowances with optional filters."""
    stmt = select(UsageAllowance)
    count_stmt = select(func.count(UsageAllowance.id))

    if is_active is not None:
        stmt = stmt.where(UsageAllowance.is_active == is_active)
        count_stmt = count_stmt.where(UsageAllowance.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = UsageAllowance.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(UsageAllowance.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def list_sla_profiles_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated SLA profiles with optional filters."""
    stmt = select(SlaProfile)
    count_stmt = select(func.count(SlaProfile.id))

    if is_active is not None:
        stmt = stmt.where(SlaProfile.is_active == is_active)
        count_stmt = count_stmt.where(SlaProfile.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = SlaProfile.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(SlaProfile.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def list_policy_sets_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated policy sets with optional filters."""
    stmt = select(PolicySet)
    count_stmt = select(func.count(PolicySet.id))

    if is_active is not None:
        stmt = stmt.where(PolicySet.is_active == is_active)
        count_stmt = count_stmt.where(PolicySet.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = PolicySet.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(PolicySet.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def list_add_ons_paginated(
    db: Session,
    *,
    is_active: bool | None,
    addon_type: str | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated add-ons with optional filters."""
    stmt = select(AddOn)
    count_stmt = select(func.count(AddOn.id))

    if is_active is not None:
        stmt = stmt.where(AddOn.is_active == is_active)
        count_stmt = count_stmt.where(AddOn.is_active == is_active)
    if addon_type:
        stmt = stmt.where(AddOn.addon_type == addon_type)
        count_stmt = count_stmt.where(AddOn.addon_type == addon_type)
    if search:
        term = f"%{search.strip()}%"
        search_filter = AddOn.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(AddOn.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


# ---------------------------------------------------------------------------
# Bulk delete helpers
# ---------------------------------------------------------------------------


def bulk_delete_region_zones(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of region zones."""
    for zone_id in ids:
        try:
            catalog_service.region_zones.delete(db=db, zone_id=zone_id)
        except Exception:
            logger.debug("Skipping region zone %s during bulk delete", zone_id)


def bulk_delete_usage_allowances(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of usage allowances."""
    for allowance_id in ids:
        try:
            catalog_service.usage_allowances.delete(db=db, allowance_id=allowance_id)
        except Exception:
            logger.debug("Skipping usage allowance %s during bulk delete", allowance_id)


def bulk_delete_sla_profiles(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of SLA profiles."""
    for profile_id in ids:
        try:
            catalog_service.sla_profiles.delete(db=db, profile_id=profile_id)
        except Exception:
            logger.debug("Skipping SLA profile %s during bulk delete", profile_id)


def bulk_delete_policy_sets(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of policy sets."""
    for policy_id in ids:
        try:
            catalog_service.policy_sets.delete(db=db, policy_id=policy_id)
        except Exception:
            logger.debug("Skipping policy set %s during bulk delete", policy_id)


def bulk_delete_add_ons(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of add-ons."""
    for addon_id in ids:
        try:
            catalog_service.add_ons.delete(db=db, add_on_id=addon_id)
        except Exception:
            logger.debug("Skipping add-on %s during bulk delete", addon_id)


# ---------------------------------------------------------------------------
# CSV export helpers
# ---------------------------------------------------------------------------


def export_region_zones_csv(db: Session) -> str:
    """Return CSV content for all region zones."""
    zones = db.scalars(
        select(RegionZone).order_by(RegionZone.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Code", "Description", "Active"])
    for z in zones:
        writer.writerow([
            str(z.id), z.name, z.code or "", z.description or "",
            "Yes" if z.is_active else "No",
        ])
    return output.getvalue()


def export_usage_allowances_csv(db: Session) -> str:
    """Return CSV content for all usage allowances."""
    allowances = db.scalars(
        select(UsageAllowance).order_by(UsageAllowance.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Name", "Included GB", "Overage Rate",
        "Overage Cap GB", "Throttle Rate Mbps", "Active",
    ])
    for a in allowances:
        writer.writerow([
            str(a.id), a.name, a.included_gb or "", a.overage_rate or "",
            a.overage_cap_gb or "", a.throttle_rate_mbps or "",
            "Yes" if a.is_active else "No",
        ])
    return output.getvalue()


def export_sla_profiles_csv(db: Session) -> str:
    """Return CSV content for all SLA profiles."""
    profiles = db.scalars(
        select(SlaProfile).order_by(SlaProfile.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Name", "Uptime %", "Response Hours",
        "Resolution Hours", "Credit %", "Notes", "Active",
    ])
    for p in profiles:
        writer.writerow([
            str(p.id), p.name, p.uptime_percent or "",
            p.response_time_hours or "", p.resolution_time_hours or "",
            p.credit_percent or "", p.notes or "",
            "Yes" if p.is_active else "No",
        ])
    return output.getvalue()


def export_policy_sets_csv(db: Session) -> str:
    """Return CSV content for all policy sets."""
    policies = db.scalars(
        select(PolicySet).order_by(PolicySet.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Name", "Proration Policy", "Downgrade Policy", "Trial Days",
        "Trial Card Required", "Grace Days", "Suspension Action",
        "Refund Policy", "Refund Window Days", "Active",
    ])
    for p in policies:
        writer.writerow([
            str(p.id), p.name,
            p.proration_policy.value if p.proration_policy else "",
            p.downgrade_policy.value if p.downgrade_policy else "",
            p.trial_days or "",
            "Yes" if p.trial_card_required else "No",
            p.grace_days or "",
            p.suspension_action.value if p.suspension_action else "",
            p.refund_policy.value if p.refund_policy else "",
            p.refund_window_days or "",
            "Yes" if p.is_active else "No",
        ])
    return output.getvalue()


def export_add_ons_csv(db: Session) -> str:
    """Return CSV content for all add-ons."""
    add_ons = db.scalars(
        select(AddOn).order_by(AddOn.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Type", "Description", "Active"])
    for a in add_ons:
        writer.writerow([
            str(a.id), a.name,
            a.addon_type.value if a.addon_type else "",
            a.description or "",
            "Yes" if a.is_active else "No",
        ])
    return output.getvalue()


# ---------------------------------------------------------------------------
# Dunning step sync (policy set create + update)
# ---------------------------------------------------------------------------


def create_dunning_steps(
    db: Session,
    policy_set_id: str,
    dunning_steps: list[dict[str, str]],
) -> None:
    """Create dunning steps for a newly-created policy set."""
    policy_uuid = coerce_uuid(policy_set_id)
    for step in dunning_steps:
        step_payload = PolicyDunningStepCreate(
            policy_set_id=policy_uuid,
            day_offset=int(step["day_offset"]),
            action=DunningAction(step["action"]),
            note=step["note"] or None,
        )
        catalog_service.policy_dunning_steps.create(db=db, payload=step_payload)


def sync_dunning_steps(
    db: Session,
    policy_id: str,
    dunning_steps: list[dict[str, str]],
) -> None:
    """Reconcile submitted dunning steps against the existing set.

    Deletes steps that were removed, updates existing ones, and creates new ones.
    """
    existing_steps = catalog_service.policy_dunning_steps.list(
        db=db, policy_set_id=policy_id,
        order_by="day_offset", order_dir="asc", limit=100, offset=0,
    )
    existing_ids = {str(s.id) for s in existing_steps}
    submitted_ids = {s["id"] for s in dunning_steps if s.get("id")}

    # Delete removed steps
    for step in existing_steps:
        if str(step.id) not in submitted_ids:
            catalog_service.policy_dunning_steps.delete(db=db, step_id=str(step.id))

    # Update or create steps
    for step in dunning_steps:
        if step.get("id") and step["id"] in existing_ids:
            catalog_service.policy_dunning_steps.update(
                db=db,
                step_id=step["id"],
                payload=PolicyDunningStepUpdate(
                    day_offset=int(step["day_offset"]),
                    action=DunningAction(step["action"]),
                    note=step["note"] or None,
                ),
            )
        else:
            catalog_service.policy_dunning_steps.create(
                db=db,
                payload=PolicyDunningStepCreate(
                    policy_set_id=coerce_uuid(policy_id),
                    day_offset=int(step["day_offset"]),
                    action=DunningAction(step["action"]),
                    note=step["note"] or None,
                ),
            )


# ---------------------------------------------------------------------------
# Add-on price sync (add-on create + update)
# ---------------------------------------------------------------------------


def create_addon_prices(
    db: Session,
    add_on_id: str,
    prices: list[dict[str, str]],
) -> None:
    """Create prices for a newly-created add-on."""
    addon_uuid = coerce_uuid(add_on_id)
    for price in prices:
        price_payload = AddOnPriceCreate(
            add_on_id=addon_uuid,
            price_type=PriceType(price["price_type"]),
            amount=Decimal(price["amount"]),
            currency=price["currency"] or "NGN",
            billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
            unit=PriceUnit(price["unit"]) if price["unit"] else None,
            description=price["description"] or None,
        )
        catalog_service.add_on_prices.create(db=db, payload=price_payload)


def sync_addon_prices(
    db: Session,
    addon_id: str,
    prices: list[dict[str, str]],
) -> None:
    """Reconcile submitted add-on prices against the existing set.

    Deletes prices that were removed, updates existing ones, and creates new ones.
    """
    existing_prices = catalog_service.add_on_prices.list(
        db=db, add_on_id=addon_id, is_active=None,
        order_by="created_at", order_dir="asc", limit=100, offset=0,
    )
    existing_ids = {str(p.id) for p in existing_prices}
    submitted_ids = {p["id"] for p in prices if p.get("id")}

    # Delete removed prices
    for price in existing_prices:
        if str(price.id) not in submitted_ids:
            catalog_service.add_on_prices.delete(db=db, price_id=str(price.id))

    # Update or create prices
    for price in prices:
        if price.get("id") and price["id"] in existing_ids:
            catalog_service.add_on_prices.update(
                db=db,
                price_id=price["id"],
                payload=AddOnPriceUpdate(
                    price_type=PriceType(price["price_type"]),
                    amount=Decimal(price["amount"]),
                    currency=price["currency"] or "NGN",
                    billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
                    unit=PriceUnit(price["unit"]) if price["unit"] else None,
                    description=price["description"] or None,
                ),
            )
        else:
            catalog_service.add_on_prices.create(
                db=db,
                payload=AddOnPriceCreate(
                    add_on_id=coerce_uuid(addon_id),
                    price_type=PriceType(price["price_type"]),
                    amount=Decimal(price["amount"]),
                    currency=price["currency"] or "NGN",
                    billing_cycle=BillingCycle(price["billing_cycle"]) if price["billing_cycle"] else None,
                    unit=PriceUnit(price["unit"]) if price["unit"] else None,
                    description=price["description"] or None,
                ),
            )
