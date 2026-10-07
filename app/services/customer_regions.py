"""Customer region configuration and geographic assignment resolver."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from geoalchemy2.types import Geography
from sqlalchemy import and_, case, cast, func, or_, select as db_select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CTE

from app.models.catalog import NasDevice, Subscription
from app.models.customer_region import CustomerRegion, CustomerRegionMatchMode
from app.models.network_monitoring import PopSite
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Address, Subscriber

REGION_MATCH_MODES = tuple(mode.value for mode in CustomerRegionMatchMode)
DEFAULT_REGION_COLOR = "#0ea5e9"
UNASSIGNED_REGION_FILTER = "unassigned"


@dataclass(frozen=True, slots=True)
class RegionOption:
    id: UUID
    name: str
    color: str


@dataclass(frozen=True, slots=True)
class RegionAssignment:
    id: UUID
    name: str
    color: str
    distance_meters: float


@dataclass(frozen=True, slots=True)
class RegionCustomerContext:
    subscriber_id: UUID
    latitude: float | None
    longitude: float | None
    pop_site_id: UUID | None = None
    nas_device_ids: frozenset[UUID] = frozenset()


def list_regions(db: Session, *, include_inactive: bool = True) -> list[CustomerRegion]:
    query = db.query(CustomerRegion).order_by(CustomerRegion.name.asc())
    if not include_inactive:
        query = query.filter(CustomerRegion.is_active.is_(True))
    return query.all()


def region_options(db: Session) -> list[RegionOption]:
    return [
        RegionOption(id=region.id, name=region.name, color=region.color)
        for region in list_regions(db, include_inactive=False)
    ]


def infrastructure_options(db: Session) -> dict[str, list[dict[str, str]]]:
    """Return bounded NAS/POP choices used by the Settings Hub form."""

    nas = (
        db.query(NasDevice.id, NasDevice.name)
        .order_by(NasDevice.name.asc())
        .limit(500)
        .all()
    )
    pop_sites = (
        db.query(PopSite.id, PopSite.name)
        .filter(PopSite.is_active.is_(True))
        .order_by(PopSite.name.asc())
        .limit(500)
        .all()
    )
    return {
        "nas": [{"id": str(row.id), "name": row.name} for row in nas],
        "pop_site": [{"id": str(row.id), "name": row.name} for row in pop_sites],
    }


def _validate_region_input(
    *,
    name: str,
    latitude: float,
    longitude: float,
    radius_meters: float,
    color: str,
    match_mode: str,
    nas_device_id: UUID | None = None,
    pop_site_id: UUID | None = None,
) -> tuple[str, float, float, float, str, str]:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Region name is required")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude or longitude is outside its valid range")
    if not 1 <= radius_meters <= 100_000:
        raise ValueError("Radius must be between 1 and 100,000 meters")
    normalized_color = color.strip().lower()
    if len(normalized_color) != 7 or not normalized_color.startswith("#"):
        raise ValueError("Region color must be a six-digit hex color")
    try:
        int(normalized_color[1:], 16)
    except ValueError as exc:
        raise ValueError("Region color must be a six-digit hex color") from exc
    normalized_mode = match_mode.strip().lower()
    if normalized_mode not in REGION_MATCH_MODES:
        raise ValueError("Unsupported region overlap mode")
    if normalized_mode == CustomerRegionMatchMode.nas.value and not nas_device_id:
        raise ValueError("Select a NAS when using the matching NAS overlap rule")
    if normalized_mode == CustomerRegionMatchMode.pop_site.value and not pop_site_id:
        raise ValueError("Select a POP/site when using the matching POP/site overlap rule")
    return (
        normalized_name,
        float(latitude),
        float(longitude),
        float(radius_meters),
        normalized_color,
        normalized_mode,
    )


def save_region(
    db: Session,
    *,
    region_id: UUID | None,
    name: str,
    latitude: float,
    longitude: float,
    radius_meters: float,
    color: str,
    match_mode: str,
    priority: int,
    nas_device_id: UUID | None,
    pop_site_id: UUID | None,
    notes: str | None,
    is_active: bool,
) -> CustomerRegion:
    values = _validate_region_input(
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        color=color,
        match_mode=match_mode,
        nas_device_id=nas_device_id,
        pop_site_id=pop_site_id,
    )
    region = db.get(CustomerRegion, region_id) if region_id else CustomerRegion()
    if region is None:
        raise ValueError("Region not found")
    (
        region.name,
        region.latitude,
        region.longitude,
        region.radius_meters,
        region.color,
        region.match_mode,
    ) = values
    region.priority = int(priority)
    region.nas_device_id = nas_device_id if region.match_mode == "nas" else None
    region.pop_site_id = pop_site_id if region.match_mode == "pop_site" else None
    region.notes = notes.strip() if notes and notes.strip() else None
    region.is_active = bool(is_active)
    if region_id is None:
        db.add(region)
    db.commit()
    db.refresh(region)
    return region


def delete_region(db: Session, *, region_id: UUID) -> None:
    region = db.get(CustomerRegion, region_id)
    if region is None:
        raise ValueError("Region not found")
    region.is_active = False
    db.commit()


def customer_region_exists_clause(region_id: str | UUID | None):
    """Return a filter for the canonical winning region of a subscriber."""

    if not region_id:
        return None
    try:
        normalized_id = UUID(str(region_id))
    except ValueError as exc:
        raise ValueError("region_id must be a valid UUID") from exc
    region_point = func.ST_SetSRID(
        func.ST_MakePoint(CustomerRegion.longitude, CustomerRegion.latitude), 4326
    )
    address_point = func.ST_SetSRID(
        func.ST_MakePoint(Address.longitude, Address.latitude), 4326
    )
    fallback_distance = func.ST_DistanceSphere(address_point, region_point)
    geography_distance = func.ST_Distance(
        cast(Address.geom, Geography), cast(region_point, Geography)
    )
    distance = case(
        (Address.geom.isnot(None), geography_distance),
        else_=fallback_distance,
    )
    within_radius = or_(
        and_(
            Address.geom.isnot(None),
            func.ST_DWithin(
                cast(Address.geom, Geography),
                cast(region_point, Geography),
                CustomerRegion.radius_meters,
            ),
        ),
        and_(
            Address.geom.is_(None),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            fallback_distance <= CustomerRegion.radius_meters,
        ),
    )
    primary_address_id = (
        db_select(Address.id)
        .where(
            Address.subscriber_id == Subscriber.id,
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
        )
        .order_by(
            case((Address.is_primary.is_(True), 0), else_=1),
            Address.id.asc(),
        )
        .limit(1)
        .correlate(Subscriber)
        .scalar_subquery()
    )
    mode_priority = case(
        (CustomerRegion.match_mode == CustomerRegionMatchMode.manual.value, 3),
        (
            CustomerRegion.match_mode.in_(
                (
                    CustomerRegionMatchMode.nas.value,
                    CustomerRegionMatchMode.pop_site.value,
                )
            ),
            2,
        ),
        (CustomerRegion.match_mode == CustomerRegionMatchMode.nearest.value, 1),
        else_=0,
    )
    infrastructure_match = or_(
        and_(
            CustomerRegion.match_mode == CustomerRegionMatchMode.nas.value,
            or_(
                Subscriber.subscriptions.any(
                    and_(
                        Subscription.provisioning_nas_device_id
                        == CustomerRegion.nas_device_id,
                        Subscription.provisioning_nas_device_id.isnot(None),
                    )
                ),
                db_select(RadiusActiveSession.id)
                .where(
                    RadiusActiveSession.subscriber_id == Subscriber.id,
                    RadiusActiveSession.nas_device_id == CustomerRegion.nas_device_id,
                    RadiusActiveSession.nas_device_id.isnot(None),
                )
                .exists(),
            ),
        ),
        and_(
            CustomerRegion.match_mode == CustomerRegionMatchMode.pop_site.value,
            CustomerRegion.pop_site_id == Subscriber.pop_site_id,
            Subscriber.pop_site_id.isnot(None),
        ),
    )
    winner_region_id = (
        db_select(CustomerRegion.id)
        .select_from(Address)
        .join(CustomerRegion, CustomerRegion.is_active.is_(True))
        .where(Address.id == primary_address_id, within_radius)
        .order_by(
            case((infrastructure_match, 0), else_=1),
            mode_priority.desc(),
            CustomerRegion.priority.desc(),
            distance.asc(),
            CustomerRegion.id.asc(),
        )
        .limit(1)
        .correlate(Subscriber)
        .scalar_subquery()
    )
    return winner_region_id == normalized_id


def customer_region_assignment_cte() -> CTE:
    """Build the canonical one-row-per-customer region assignment relation.

    The CTE deliberately mirrors :func:`customer_region_exists_clause` so
    reports can aggregate by the winning region in one bounded SQL pipeline
    instead of resolving every customer in Python.  Callers must still apply
    their own visibility and date/status predicates to the resulting relation.
    """

    region_point = func.ST_SetSRID(
        func.ST_MakePoint(CustomerRegion.longitude, CustomerRegion.latitude), 4326
    )
    address_point = func.ST_SetSRID(
        func.ST_MakePoint(Address.longitude, Address.latitude), 4326
    )
    fallback_distance = func.ST_DistanceSphere(address_point, region_point)
    geography_distance = func.ST_Distance(
        cast(Address.geom, Geography), cast(region_point, Geography)
    )
    distance = case(
        (Address.geom.isnot(None), geography_distance),
        else_=fallback_distance,
    )
    within_radius = or_(
        and_(
            Address.geom.isnot(None),
            func.ST_DWithin(
                cast(Address.geom, Geography),
                cast(region_point, Geography),
                CustomerRegion.radius_meters,
            ),
        ),
        and_(
            Address.geom.is_(None),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            fallback_distance <= CustomerRegion.radius_meters,
        ),
    )
    primary_address_id = (
        db_select(Address.id)
        .where(
            Address.subscriber_id == Subscriber.id,
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
        )
        .order_by(
            case((Address.is_primary.is_(True), 0), else_=1),
            Address.id.asc(),
        )
        .limit(1)
        .correlate(Subscriber)
        .scalar_subquery()
    )
    mode_priority = case(
        (CustomerRegion.match_mode == CustomerRegionMatchMode.manual.value, 3),
        (
            CustomerRegion.match_mode.in_(
                (
                    CustomerRegionMatchMode.nas.value,
                    CustomerRegionMatchMode.pop_site.value,
                )
            ),
            2,
        ),
        (CustomerRegion.match_mode == CustomerRegionMatchMode.nearest.value, 1),
        else_=0,
    )
    infrastructure_match = or_(
        and_(
            CustomerRegion.match_mode == CustomerRegionMatchMode.nas.value,
            or_(
                Subscriber.subscriptions.any(
                    and_(
                        Subscription.provisioning_nas_device_id
                        == CustomerRegion.nas_device_id,
                        Subscription.provisioning_nas_device_id.isnot(None),
                    )
                ),
                db_select(RadiusActiveSession.id)
                .where(
                    RadiusActiveSession.subscriber_id == Subscriber.id,
                    RadiusActiveSession.nas_device_id == CustomerRegion.nas_device_id,
                    RadiusActiveSession.nas_device_id.isnot(None),
                )
                .exists(),
            ),
        ),
        and_(
            CustomerRegion.match_mode == CustomerRegionMatchMode.pop_site.value,
            CustomerRegion.pop_site_id == Subscriber.pop_site_id,
            Subscriber.pop_site_id.isnot(None),
        ),
    )
    assignment_rank = func.row_number().over(
        partition_by=Subscriber.id,
        order_by=(
            case((infrastructure_match, 0), else_=1),
            mode_priority.desc(),
            CustomerRegion.priority.desc(),
            distance.asc(),
            CustomerRegion.id.asc(),
        ),
    ).label("assignment_rank")
    return (
        db_select(
            Subscriber.id.label("subscriber_id"),
            CustomerRegion.id.label("region_id"),
            CustomerRegion.name.label("region_name"),
            CustomerRegion.color.label("region_color"),
            distance.label("distance_meters"),
            assignment_rank,
        )
        .select_from(Subscriber)
        .join(Address, Address.id == primary_address_id)
        .join(CustomerRegion, CustomerRegion.is_active.is_(True))
        .where(within_radius)
        .cte("customer_region_assignments")
    )
def customer_region_filter_clause(region_id: str | UUID | None):
    """Return the canonical customer-list predicate for a region filter.

    ``unassigned`` is deliberately a transport sentinel rather than a fake
    UUID. It selects customers for whom the same winning-region relation used
    by the configured-region filter has no row, so the list and reports share
    one assignment definition.
    """

    if str(region_id or "").strip().lower() == UNASSIGNED_REGION_FILTER:
        assignments = customer_region_assignment_cte()
        assigned_subscriber_ids = db_select(assignments.c.subscriber_id).where(
            assignments.c.assignment_rank == 1
        )
        return ~Subscriber.id.in_(assigned_subscriber_ids)
    return customer_region_exists_clause(region_id)


def primary_geocoded_address(addresses: Iterable[Address]) -> Address | None:
    """Return the stable address used for customer region classification."""

    valid = [
        address
        for address in addresses
        if address.latitude is not None and address.longitude is not None
    ]
    if not valid:
        return None
    return min(
        valid,
        key=lambda address: (not bool(address.is_primary), str(address.id)),
    )


def _distance_meters(
    latitude: float, longitude: float, region: CustomerRegion
) -> float:
    radius = 6_371_000.0
    lat1, lat2 = math.radians(latitude), math.radians(float(region.latitude))
    delta_lat = lat2 - lat1
    delta_lon = math.radians(float(region.longitude) - longitude)
    value = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(
        lat2
    ) * math.sin(delta_lon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def resolve_region(
    regions: Iterable[CustomerRegion],
    *,
    latitude: float | None,
    longitude: float | None,
    pop_site_id: UUID | None = None,
    nas_device_ids: frozenset[UUID] = frozenset(),
) -> RegionAssignment | None:
    if latitude is None or longitude is None:
        return None
    candidates: list[tuple[CustomerRegion, float]] = []
    for region in regions:
        distance = _distance_meters(float(latitude), float(longitude), region)
        if distance <= float(region.radius_meters):
            candidates.append((region, distance))
    if not candidates:
        return None

    matched = [
        candidate
        for candidate in candidates
        if (
            candidate[0].match_mode == CustomerRegionMatchMode.nas.value
            and candidate[0].nas_device_id in nas_device_ids
        )
        or (
            candidate[0].match_mode == CustomerRegionMatchMode.pop_site.value
            and candidate[0].pop_site_id == pop_site_id
        )
    ]
    if matched:
        candidates = matched
    mode_priority = {
        CustomerRegionMatchMode.manual.value: 3,
        CustomerRegionMatchMode.nas.value: 2,
        CustomerRegionMatchMode.pop_site.value: 2,
        CustomerRegionMatchMode.nearest.value: 1,
    }
    selected, distance = min(
        candidates,
        key=lambda candidate: (
            -mode_priority.get(candidate[0].match_mode, 0),
            -int(candidate[0].priority),
            candidate[1],
            str(candidate[0].id),
        ),
    )
    return RegionAssignment(
        id=selected.id,
        name=selected.name,
        color=selected.color,
        distance_meters=round(distance, 2),
    )


def assign_regions(
    db: Session, contexts: Iterable[RegionCustomerContext]
) -> dict[UUID, RegionAssignment]:
    regions = list_regions(db, include_inactive=False)
    return {
        context.subscriber_id: assignment
        for context in contexts
        if (assignment := resolve_region(
            regions,
            latitude=context.latitude,
            longitude=context.longitude,
            pop_site_id=context.pop_site_id,
            nas_device_ids=context.nas_device_ids,
        ))
        is not None
    }
