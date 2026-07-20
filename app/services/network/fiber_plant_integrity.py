"""Operational integrity and capacity controls for canonical passive plant.

This module is a validator/projection used by the reviewed fiber-change owner.
It never chooses endpoint identity and never writes cable edges. Active cable
segments must be exact, sized, and connected to a serving PON-rooted component.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    Splitter,
    SplitterPort,
    SplitterPortType,
)


class FiberPlantIntegrityError(ValueError):
    """Raised when an operational plant mutation would violate SOT controls."""


OPERATIONAL_ENDPOINT_MODELS: dict[ODNEndpointType, type] = {
    ODNEndpointType.fdh: FdhCabinet,
    ODNEndpointType.fiber_access_point: FiberAccessPoint,
    ODNEndpointType.ont: OntUnit,
    ODNEndpointType.pon_port: PonPort,
    ODNEndpointType.splice_closure: FiberSpliceClosure,
    ODNEndpointType.splitter_port: SplitterPort,
}


@dataclass(frozen=True)
class CableCapacity:
    segment_id: uuid.UUID
    total_fibers: int
    modeled_fibers: int
    available_fibers: int
    reserved_fibers: int
    in_use_fibers: int
    damaged_fibers: int
    retired_fibers: int
    complete: bool

    @property
    def unmodeled_fibers(self) -> int:
        return max(self.total_fibers - self.modeled_fibers, 0)


@dataclass(frozen=True)
class SplitterCapacity:
    splitter_id: uuid.UUID
    input_capacity: int
    output_capacity: int
    modeled_inputs: int
    modeled_outputs: int
    occupied_outputs: int

    @property
    def spare_outputs(self) -> int:
        return max(self.output_capacity - self.occupied_outputs, 0)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _endpoint_type(point: FiberTerminationPoint) -> ODNEndpointType:
    value = point.endpoint_type
    if isinstance(value, ODNEndpointType):
        return value
    try:
        return ODNEndpointType(str(value))
    except ValueError as exc:
        raise FiberPlantIntegrityError(
            "active cable termination has an unsupported endpoint type"
        ) from exc


def validate_operational_termination(
    db: Session, point: FiberTerminationPoint
) -> object:
    """Require an active termination to name one exact active infrastructure row."""

    if not point.is_active:
        raise FiberPlantIntegrityError("operational cable termination is inactive")
    endpoint_type = _endpoint_type(point)
    model = OPERATIONAL_ENDPOINT_MODELS.get(endpoint_type)
    if model is None or point.ref_id is None:
        raise FiberPlantIntegrityError(
            "operational cable termination must reference approved infrastructure"
        )
    endpoint = db.get(model, point.ref_id)
    if endpoint is None or getattr(endpoint, "is_active", True) is False:
        raise FiberPlantIntegrityError(
            "operational cable termination references missing or inactive infrastructure"
        )
    if isinstance(endpoint, SplitterPort):
        splitter = db.get(Splitter, endpoint.splitter_id)
        if splitter is None or not splitter.is_active:
            raise FiberPlantIntegrityError(
                "operational splitter-port termination has no active splitter"
            )
    if isinstance(endpoint, PonPort):
        olt = db.get(OLTDevice, endpoint.olt_id)
        if olt is None or not olt.is_active:
            raise FiberPlantIntegrityError(
                "operational PON termination has no active serving OLT"
            )
    return endpoint


def _segment_shape(segment: FiberSegment) -> tuple[uuid.UUID, uuid.UUID]:
    if (
        segment.from_point_id is None
        or segment.to_point_id is None
        or segment.from_point_id == segment.to_point_id
        or segment.route_geom is None
        or segment.fiber_count is None
        or segment.fiber_count <= 0
    ):
        raise FiberPlantIntegrityError(
            "active cable requires two distinct terminations, approved geometry, "
            "and a positive fiber_count"
        )
    return segment.from_point_id, segment.to_point_id


def _active_segments(
    db: Session,
    *,
    replacement: FiberSegment | None = None,
    omit_id: uuid.UUID | None = None,
) -> list[FiberSegment]:
    with db.no_autoflush:
        rows = list(
            db.scalars(
                select(FiberSegment)
                .where(FiberSegment.is_active.is_(True))
                .order_by(FiberSegment.id)
            ).all()
        )
    result = [row for row in rows if row.id != omit_id]
    if replacement is not None and replacement.is_active:
        result = [row for row in result if row.id != replacement.id]
        result.append(replacement)
    return result


def _adjacency(
    segments: list[FiberSegment],
) -> tuple[
    dict[uuid.UUID, set[uuid.UUID]],
    dict[uuid.UUID, list[FiberSegment]],
]:
    neighbors: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    incident: dict[uuid.UUID, list[FiberSegment]] = defaultdict(list)
    for segment in segments:
        start_id, end_id = _segment_shape(segment)
        neighbors[start_id].add(end_id)
        neighbors[end_id].add(start_id)
        incident[start_id].append(segment)
        incident[end_id].append(segment)
    return neighbors, incident


def _component(
    start: uuid.UUID, neighbors: dict[uuid.UUID, set[uuid.UUID]]
) -> set[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    pending: deque[uuid.UUID] = deque([start])
    while pending:
        point_id = pending.popleft()
        if point_id in seen:
            continue
        seen.add(point_id)
        pending.extend(neighbors.get(point_id, set()) - seen)
    return seen


def _validated_points(
    db: Session, point_ids: set[uuid.UUID]
) -> dict[uuid.UUID, FiberTerminationPoint]:
    if not point_ids:
        return {}
    with db.no_autoflush:
        points = {
            point.id: point
            for point in db.scalars(
                select(FiberTerminationPoint).where(
                    FiberTerminationPoint.id.in_(point_ids)
                )
            ).all()
        }
    if set(points) != point_ids:
        raise FiberPlantIntegrityError(
            "active cable component references a missing termination"
        )
    for point in points.values():
        validate_operational_termination(db, point)
    return points


def _is_root(point: FiberTerminationPoint) -> bool:
    return _endpoint_type(point) == ODNEndpointType.pon_port


def validate_active_segment(db: Session, segment: FiberSegment) -> None:
    """Validate one active candidate and its complete exact PON-rooted component."""

    if not segment.is_active:
        return
    start_id, _end_id = _segment_shape(segment)
    segments = _active_segments(db, replacement=segment)
    neighbors, _incident = _adjacency(segments)
    component = _component(start_id, neighbors)
    points = _validated_points(db, component)
    if not any(_is_root(point) for point in points.values()):
        raise FiberPlantIntegrityError(
            "active cable component must resolve to an exact serving PON/OLT root"
        )


def _customer_bearing_ont_ids(
    db: Session, points: dict[uuid.UUID, FiberTerminationPoint]
) -> set[uuid.UUID]:
    ont_ids = {
        point.ref_id
        for point in points.values()
        if _endpoint_type(point) == ODNEndpointType.ont and point.ref_id is not None
    }
    if not ont_ids:
        return set()
    with db.no_autoflush:
        return set(
            db.scalars(
                select(OntAssignment.ont_unit_id).where(
                    OntAssignment.active.is_(True),
                    OntAssignment.ont_unit_id.in_(ont_ids),
                )
            ).all()
        )


def validate_segment_retirement(db: Session, segment: FiberSegment) -> None:
    """Reject removal that leaves cable or active customer plant unrooted."""

    if not segment.is_active:
        return
    start_id, end_id = _segment_shape(segment)
    before = _active_segments(db)
    before_neighbors, _before_incident = _adjacency(before)
    affected_ids = _component(start_id, before_neighbors) | _component(
        end_id, before_neighbors
    )
    affected_points = _validated_points(db, affected_ids)

    after = _active_segments(db, omit_id=segment.id)
    after_neighbors, after_incident = _adjacency(after)
    checked: set[uuid.UUID] = set()
    rooted_points: set[uuid.UUID] = set()
    for point_id in affected_ids:
        if point_id in checked or not after_incident.get(point_id):
            continue
        component = _component(point_id, after_neighbors)
        checked.update(component)
        points = _validated_points(db, component)
        if not any(_is_root(point) for point in points.values()):
            raise FiberPlantIntegrityError(
                "segment retirement would orphan an active cable component"
            )
        rooted_points.update(component)

    customer_onts = _customer_bearing_ont_ids(db, affected_points)
    for point_id, point in affected_points.items():
        if (
            _endpoint_type(point) == ODNEndpointType.ont
            and point.ref_id in customer_onts
            and point_id not in rooted_points
        ):
            raise FiberPlantIntegrityError(
                "segment retirement would detach an active customer ONT from its PON root"
            )


def validate_termination_change(
    db: Session,
    point: FiberTerminationPoint,
    *,
    changes: dict[str, object] | None = None,
) -> None:
    """Protect connected termination identity and activation state."""

    connected = bool(
        db.scalar(
            select(FiberSegment.id).where(
                FiberSegment.is_active.is_(True),
                or_(
                    FiberSegment.from_point_id == point.id,
                    FiberSegment.to_point_id == point.id,
                ),
            )
        )
    )
    changes = changes or {}
    endpoint_type = changes.get("endpoint_type", point.endpoint_type)
    next_type = _enum_value(endpoint_type)
    current_type = _enum_value(point.endpoint_type)
    next_ref = changes.get("ref_id", point.ref_id)
    next_active = bool(changes.get("is_active", point.is_active))
    if connected and (
        next_type != current_type or next_ref != point.ref_id or not next_active
    ):
        raise FiberPlantIntegrityError(
            "connected termination identity cannot change or retire before its cables"
        )


def normalize_splitter_capacity(
    *,
    input_ports: int,
    output_ports: int,
    splitter_ratio: str | None,
    is_active: bool,
) -> str | None:
    if input_ports < 1 or output_ports < 1:
        raise FiberPlantIntegrityError("splitter capacity must be positive")
    if not is_active:
        return splitter_ratio
    expected = f"{input_ports}:{output_ports}"
    if (splitter_ratio or "").strip() != expected:
        raise FiberPlantIntegrityError(
            f"active splitter ratio must be the exact declared capacity {expected}"
        )
    return expected


def validate_splitter_capacity(db: Session, splitter: Splitter) -> None:
    normalize_splitter_capacity(
        input_ports=splitter.input_ports,
        output_ports=splitter.output_ports,
        splitter_ratio=splitter.splitter_ratio,
        is_active=splitter.is_active,
    )
    if not splitter.is_active or splitter.id is None:
        return
    with db.no_autoflush:
        counts: dict[SplitterPortType, int] = {}
        for port_type, count in db.execute(
            select(SplitterPort.port_type, func.count(SplitterPort.id))
            .where(
                SplitterPort.splitter_id == splitter.id,
                SplitterPort.is_active.is_(True),
            )
            .group_by(SplitterPort.port_type)
        ):
            counts[port_type] = int(count)
    inputs = int(counts.get(SplitterPortType.input, 0))
    outputs = int(counts.get(SplitterPortType.output, 0))
    if inputs > splitter.input_ports or outputs > splitter.output_ports:
        raise FiberPlantIntegrityError(
            "splitter capacity cannot be smaller than its active modeled ports"
        )


def validate_splitter_port_capacity(
    db: Session, port: SplitterPort, *, omit_id: uuid.UUID | None = None
) -> None:
    if not port.is_active:
        return
    splitter = db.get(Splitter, port.splitter_id)
    if splitter is None or not splitter.is_active:
        raise FiberPlantIntegrityError(
            "active splitter port requires an active splitter"
        )
    validate_splitter_capacity(db, splitter)
    port_type = (
        port.port_type
        if isinstance(port.port_type, SplitterPortType)
        else SplitterPortType(str(port.port_type))
    )
    limit = (
        splitter.input_ports
        if port_type == SplitterPortType.input
        else splitter.output_ports
    )
    with db.no_autoflush:
        statement = select(func.count(SplitterPort.id)).where(
            SplitterPort.splitter_id == splitter.id,
            SplitterPort.port_type == port_type,
            SplitterPort.is_active.is_(True),
        )
        if omit_id is not None:
            statement = statement.where(SplitterPort.id != omit_id)
        existing = int(db.scalar(statement) or 0)
    if existing + 1 > limit:
        raise FiberPlantIntegrityError(
            f"splitter {port_type.value} port capacity {limit} would be exceeded"
        )


def ensure_segment_strand_inventory(db: Session, segment: FiberSegment) -> None:
    """Materialize exact numbered fibers for a newly approved active cable."""

    if not segment.is_active:
        return
    if segment.id is None or segment.fiber_count is None:
        raise FiberPlantIntegrityError("active cable capacity is incomplete")
    with db.no_autoflush:
        legacy_name_only = db.scalar(
            select(FiberStrand.id).where(
                FiberStrand.segment_id.is_(None),
                FiberStrand.cable_name == segment.name,
            )
        )
        if legacy_name_only is not None:
            raise FiberPlantIntegrityError(
                "legacy cable-name strands require a reviewed exact segment "
                "assignment before core inventory can be materialized"
            )
        strands = list(
            db.scalars(
                select(FiberStrand)
                .where(FiberStrand.segment_id == segment.id)
                .order_by(FiberStrand.strand_number)
            ).all()
        )
    existing = {strand.strand_number for strand in strands}
    if any(number < 1 or number > segment.fiber_count for number in existing):
        raise FiberPlantIntegrityError(
            "cable size is smaller than its exact numbered fiber inventory"
        )
    for number in range(1, segment.fiber_count + 1):
        if number in existing:
            continue
        db.add(
            FiberStrand(
                cable_name=segment.name,
                segment_id=segment.id,
                strand_number=number,
                status=FiberStrandStatus.available,
                is_active=True,
            )
        )
    for strand in strands:
        strand.cable_name = segment.name


def validate_strand_segment_capacity(db: Session, strand: FiberStrand) -> None:
    if strand.segment_id is None:
        return
    segment = db.get(FiberSegment, strand.segment_id)
    if segment is None:
        raise FiberPlantIntegrityError(
            "fiber strand references a missing cable segment"
        )
    if segment.fiber_count is None or not (
        1 <= strand.strand_number <= segment.fiber_count
    ):
        raise FiberPlantIntegrityError(
            "fiber strand number exceeds the cable's declared fiber_count"
        )
    strand.cable_name = segment.name


def validate_strand_retirement(db: Session, strand: FiberStrand) -> None:
    if strand.segment_id is None:
        return
    segment = db.get(FiberSegment, strand.segment_id)
    if segment is not None and segment.is_active:
        raise FiberPlantIntegrityError(
            "an exact fiber core cannot be removed from an active sized cable; "
            "record its damaged or retired status instead"
        )


def cable_capacity(db: Session, segment_id: uuid.UUID) -> CableCapacity:
    segment = db.get(FiberSegment, segment_id)
    if segment is None or segment.fiber_count is None:
        raise FiberPlantIntegrityError("cable segment capacity is unavailable")
    rows = list(
        db.execute(
            select(FiberStrand.status, func.count(FiberStrand.id))
            .where(FiberStrand.segment_id == segment.id)
            .group_by(FiberStrand.status)
        ).all()
    )
    counts = {_enum_value(status): int(count) for status, count in rows}
    modeled = sum(counts.values())
    return CableCapacity(
        segment_id=segment.id,
        total_fibers=segment.fiber_count,
        modeled_fibers=modeled,
        available_fibers=counts.get(FiberStrandStatus.available.value, 0),
        reserved_fibers=counts.get(FiberStrandStatus.reserved.value, 0),
        in_use_fibers=counts.get(FiberStrandStatus.in_use.value, 0),
        damaged_fibers=counts.get(FiberStrandStatus.damaged.value, 0),
        retired_fibers=counts.get(FiberStrandStatus.retired.value, 0),
        complete=(
            modeled == segment.fiber_count
            and all(
                1 <= strand.strand_number <= segment.fiber_count
                for strand in db.scalars(
                    select(FiberStrand).where(FiberStrand.segment_id == segment.id)
                )
            )
        ),
    )


def splitter_capacity(db: Session, splitter_id: uuid.UUID) -> SplitterCapacity:
    splitter = db.get(Splitter, splitter_id)
    if splitter is None:
        raise FiberPlantIntegrityError("splitter not found")
    ports = list(
        db.scalars(
            select(SplitterPort).where(
                SplitterPort.splitter_id == splitter.id,
                SplitterPort.is_active.is_(True),
            )
        ).all()
    )
    inputs = sum(port.port_type == SplitterPortType.input for port in ports)
    outputs = [port for port in ports if port.port_type == SplitterPortType.output]
    output_ids = [port.id for port in outputs]
    occupied = 0
    if output_ids:
        occupied = int(
            db.scalar(
                select(func.count(func.distinct(OntUnit.splitter_port_id))).where(
                    OntUnit.is_active.is_(True),
                    OntUnit.splitter_port_id.in_(output_ids),
                )
            )
            or 0
        )
    return SplitterCapacity(
        splitter_id=splitter.id,
        input_capacity=splitter.input_ports,
        output_capacity=splitter.output_ports,
        modeled_inputs=inputs,
        modeled_outputs=len(outputs),
        occupied_outputs=occupied,
    )


__all__ = [
    "CableCapacity",
    "FiberPlantIntegrityError",
    "OPERATIONAL_ENDPOINT_MODELS",
    "SplitterCapacity",
    "cable_capacity",
    "ensure_segment_strand_inventory",
    "normalize_splitter_capacity",
    "splitter_capacity",
    "validate_active_segment",
    "validate_operational_termination",
    "validate_segment_retirement",
    "validate_splitter_capacity",
    "validate_splitter_port_capacity",
    "validate_strand_segment_capacity",
    "validate_strand_retirement",
    "validate_termination_change",
]
