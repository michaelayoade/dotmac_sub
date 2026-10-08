"""Customer region configuration and geographic assignment resolver."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from geoalchemy2.types import Geography
from sqlalchemy import and_, case, cast, func, or_
from sqlalchemy import select as db_select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import CTE

from app.models.catalog import NasDevice, Subscription
from app.models.customer_region import CustomerRegion, CustomerRegionMatchMode
from app.models.network import OntAssignment
from app.models.network_monitoring import PopSite
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Address, Subscriber, SubscriberStatus
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

REGION_MATCH_MODES = tuple(mode.value for mode in CustomerRegionMatchMode)
DEFAULT_REGION_COLOR = "#0ea5e9"
UNASSIGNED_REGION_FILTER = "unassigned"
WRITE_SCOPE = "gis:area:write"
_SAVE_REGION = OwnerCommandDefinition(
    owner="gis.customer_regions",
    concern="customer region configuration",
    name="save_customer_region",
)
_DISABLE_REGION = OwnerCommandDefinition(
    owner="gis.customer_regions",
    concern="customer region configuration",
    name="disable_customer_region",
)


class CustomerRegionError(DomainError):
    """Stable transport-neutral customer-region failure."""


def _error(suffix: str, message: str, **details: object) -> CustomerRegionError:
    return CustomerRegionError(
        code=f"gis.customer_regions.{suffix}",
        message=message,
        details=details,
    )


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


@dataclass(frozen=True, slots=True)
class SaveCustomerRegionCommand:
    context: CommandContext
    region_id: UUID | None
    name: str
    latitude: float
    longitude: float
    radius_meters: float
    color: str
    match_mode: str
    priority: int
    nas_device_id: UUID | None
    pop_site_id: UUID | None
    notes: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class DisableCustomerRegionCommand:
    context: CommandContext
    region_id: UUID


@dataclass(frozen=True, slots=True)
class CustomerRegionOutcome:
    region_id: UUID
    name: str
    is_active: bool
    created: bool


@dataclass(frozen=True, slots=True)
class CustomerMapAddressObservation:
    address_id: UUID
    address_line1: str | None
    city: str | None
    latitude: float
    longitude: float
    first_name: str | None
    last_name: str | None
    subscriber_id: UUID
    pop_site_id: UUID | None
    customer_status: SubscriberStatus | None


def customer_map_address_observations(
    db: Session, *, limit: int
) -> list[CustomerMapAddressObservation]:
    """Load the bounded authoritative customer-location cohort for map views."""

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
    assigned_service_addresses = db_select(OntAssignment.service_address_id).where(
        OntAssignment.active.is_(True),
        OntAssignment.service_address_id.isnot(None),
    )
    rows = (
        db.query(
            Address.id,
            Address.address_line1,
            Address.city,
            Address.latitude,
            Address.longitude,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.id.label("subscriber_id"),
            Subscriber.pop_site_id,
            Subscriber.status.label("customer_status"),
        )
        .join(Subscriber, Address.subscriber_id == Subscriber.id)
        .filter(
            Address.id == primary_address_id,
            Address.id.in_(assigned_service_addresses),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
            Subscriber.is_active.is_(True),
        )
        .order_by(Address.id)
        .limit(limit)
        .all()
    )
    return [
        CustomerMapAddressObservation(
            address_id=row.id,
            address_line1=row.address_line1,
            city=row.city,
            latitude=float(row.latitude),
            longitude=float(row.longitude),
            first_name=row.first_name,
            last_name=row.last_name,
            subscriber_id=row.subscriber_id,
            pop_site_id=row.pop_site_id,
            customer_status=row.customer_status,
        )
        for row in rows
    ]


def customer_map_subscriptions(
    db: Session, *, subscriber_ids: frozenset[UUID]
) -> list[Subscription]:
    """Load subscriptions used by the canonical customer-map observations."""

    if not subscriber_ids:
        return []
    return (
        db.query(Subscription)
        .filter(Subscription.subscriber_id.in_(subscriber_ids))
        .order_by(Subscription.id)
        .all()
    )


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


def infrastructure_options(
    db: Session,
    *,
    nas_device_id: UUID | None = None,
    pop_site_id: UUID | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Return only selected infrastructure labels for lazy typeahead fields."""

    nas = (
        db.query(NasDevice.id, NasDevice.name)
        .filter(NasDevice.id == nas_device_id)
        .order_by(NasDevice.name.asc())
        .all()
        if nas_device_id is not None
        else []
    )
    pop_sites = (
        db.query(PopSite.id, PopSite.name)
        .filter(PopSite.is_active.is_(True), PopSite.id == pop_site_id)
        .order_by(PopSite.name.asc())
        .all()
        if pop_site_id is not None
        else []
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
        raise _error("invalid_region", "Region name is required", field="name")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise _error(
            "invalid_region",
            "Latitude or longitude is outside its valid range",
            field="coordinates",
        )
    if not 1 <= radius_meters <= 100_000:
        raise _error(
            "invalid_region",
            "Radius must be between 1 and 100,000 meters",
            field="radius_meters",
        )
    normalized_color = color.strip().lower()
    if len(normalized_color) != 7 or not normalized_color.startswith("#"):
        raise _error(
            "invalid_region",
            "Region color must be a six-digit hex color",
            field="color",
        )
    try:
        int(normalized_color[1:], 16)
    except ValueError as exc:
        raise _error(
            "invalid_region",
            "Region color must be a six-digit hex color",
            field="color",
        ) from exc
    normalized_mode = match_mode.strip().lower()
    if normalized_mode not in REGION_MATCH_MODES:
        raise _error(
            "invalid_region",
            "Unsupported region overlap mode",
            field="match_mode",
        )
    if normalized_mode == CustomerRegionMatchMode.nas.value and not nas_device_id:
        raise _error(
            "invalid_region",
            "Select a NAS when using the matching NAS overlap rule",
            field="nas_device_id",
        )
    if normalized_mode == CustomerRegionMatchMode.pop_site.value and not pop_site_id:
        raise _error(
            "invalid_region",
            "Select a POP/site when using the matching POP/site overlap rule",
            field="pop_site_id",
        )
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
    command: SaveCustomerRegionCommand,
) -> CustomerRegionOutcome:
    """Create or update one region in the owner's atomic transaction."""

    def operation() -> CustomerRegionOutcome:
        values = _validate_region_input(
            name=command.name,
            latitude=command.latitude,
            longitude=command.longitude,
            radius_meters=command.radius_meters,
            color=command.color,
            match_mode=command.match_mode,
            nas_device_id=command.nas_device_id,
            pop_site_id=command.pop_site_id,
        )
        created = command.region_id is None
        region: CustomerRegion | None
        if command.region_id is None:
            region = CustomerRegion()
        else:
            region = db.scalar(
                db_select(CustomerRegion)
                .where(CustomerRegion.id == command.region_id)
                .with_for_update()
            )
        if region is None:
            raise _error(
                "region_not_found",
                "Region not found",
                region_id=str(command.region_id),
            )
        (
            region.name,
            region.latitude,
            region.longitude,
            region.radius_meters,
            region.color,
            region.match_mode,
        ) = values
        region.priority = int(command.priority)
        region.nas_device_id = (
            command.nas_device_id if region.match_mode == "nas" else None
        )
        region.pop_site_id = (
            command.pop_site_id if region.match_mode == "pop_site" else None
        )
        region.notes = (
            command.notes.strip() if command.notes and command.notes.strip() else None
        )
        region.is_active = bool(command.is_active)
        if created:
            db.add(region)
        db.flush()
        emit_event(
            db,
            EventType.customer_region_changed,
            {
                "region_id": str(region.id),
                "change": "created" if created else "updated",
                "is_active": region.is_active,
                "command_id": str(command.context.command_id),
            },
            actor=command.context.actor,
        )
        return CustomerRegionOutcome(
            region_id=region.id,
            name=region.name,
            is_active=region.is_active,
            created=created,
        )

    try:
        return execute_owner_command(
            db,
            definition=_SAVE_REGION,
            context=command.context,
            operation=operation,
        )
    except IntegrityError as exc:
        raise _error(
            "duplicate_name",
            "A region with this name already exists. Choose a unique name.",
            field="name",
        ) from exc


def delete_region(
    db: Session, command: DisableCustomerRegionCommand
) -> CustomerRegionOutcome:
    """Disable a region while retaining its configuration and evidence."""

    def operation() -> CustomerRegionOutcome:
        region = db.scalar(
            db_select(CustomerRegion)
            .where(CustomerRegion.id == command.region_id)
            .with_for_update()
        )
        if region is None:
            raise _error(
                "region_not_found",
                "Region not found",
                region_id=str(command.region_id),
            )
        replayed = not region.is_active
        region.is_active = False
        db.flush()
        if not replayed:
            emit_event(
                db,
                EventType.customer_region_changed,
                {
                    "region_id": str(region.id),
                    "change": "disabled",
                    "is_active": False,
                    "command_id": str(command.context.command_id),
                },
                actor=command.context.actor,
            )
        return CustomerRegionOutcome(
            region_id=region.id,
            name=region.name,
            is_active=False,
            created=False,
        )

    return execute_owner_command(
        db,
        definition=_DISABLE_REGION,
        context=command.context,
        operation=operation,
    )


def get_region(db: Session, *, region_id: UUID) -> CustomerRegion | None:
    """Return one configured region for the read-only admin adapter."""

    return db.get(CustomerRegion, region_id)


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
    assignment_rank = (
        func.row_number()
        .over(
            partition_by=Subscriber.id,
            order_by=(
                case((infrastructure_match, 0), else_=1),
                mode_priority.desc(),
                CustomerRegion.priority.desc(),
                distance.asc(),
                CustomerRegion.id.asc(),
            ),
        )
        .label("assignment_rank")
    )
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
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
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
        if (
            assignment := resolve_region(
                regions,
                latitude=context.latitude,
                longitude=context.longitude,
                pop_site_id=context.pop_site_id,
                nas_device_ids=context.nas_device_ids,
            )
        )
        is not None
    }
