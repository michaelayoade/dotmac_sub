"""Canonical fiber-topology integrity and cutover readiness owner.

The service is deliberately read-only in the first cutover slice.  It names the
authoritative edges, measures legacy/fallback drift, and exposes the gates that
must pass before imported map geometry can become operational topology.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.elements import ColumnElement

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberTerminationPoint,
    ODNEndpointType,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
    SplitterPortType,
)
from app.models.network_monitoring import NetworkDevice
from app.models.subscriber import Subscriber
from app.services.network.identity import identity_for_ont_assignment


@dataclass(frozen=True)
class FiberTopologyInventory:
    active_olts: int
    active_pon_ports: int
    active_onts: int
    active_ont_assignments: int
    active_fdh_cabinets: int
    active_splitters: int
    active_splitter_ports: int
    active_splitter_port_assignments: int
    active_pon_splitter_links: int
    active_access_points: int
    active_splice_closures: int
    splice_trays: int
    splices: int
    active_strands: int
    active_termination_points: int
    active_segments: int


@dataclass(frozen=True)
class ElectronicPathIntegrity:
    active_fiber_subscriptions: int
    exact_subscription_assignments: int
    subscriber_fallback_assignments: int
    assignments_with_service_address: int
    active_onts_with_pon: int
    active_onts_with_splitter_port: int
    subscriptions_traceable_to_splitter: int
    onts_on_wrong_olt_pon: int
    assignment_pon_disagrees_with_ont: int
    assignments_on_wrong_olt_pon: int
    assignments_to_inactive_ont: int
    assignments_to_inactive_pon: int
    subscriptions_with_multiple_assignments: int
    subscribers_with_multiple_assignments: int
    active_olts_with_monitoring_node: int
    active_olts_with_pop_site: int


@dataclass(frozen=True)
class PassivePlantIntegrity:
    fdh_with_coordinates: int
    splitters_with_fdh: int
    pon_links_to_input_port: int
    pon_links_to_non_input_port: int
    ont_links_to_output_port: int
    ont_links_to_non_output_port: int
    strands_with_both_endpoints: int
    terminations_with_asset_reference: int
    segments_with_both_endpoints: int
    segments_with_route_geometry: int
    connected_segments_with_geometry: int


@dataclass(frozen=True)
class FiberTopologyFinding:
    code: str
    severity: str
    count: int
    message: str


@dataclass(frozen=True)
class FiberTopologyAudit:
    inventory: FiberTopologyInventory
    electronic: ElectronicPathIntegrity
    passive: PassivePlantIntegrity
    findings: tuple[FiberTopologyFinding, ...]
    trace_coverage: FiberTraceCoverage | None = None

    @property
    def aggregate_preconditions_ready(self) -> bool:
        """Whether inventory-level blockers are clear (not a cutover verdict)."""
        return not any(finding.severity == "blocker" for finding in self.findings)

    @property
    def customer_trace_cutover_ready(self) -> bool:
        coverage = self.trace_coverage
        return bool(
            self.aggregate_preconditions_ready
            and coverage is not None
            and coverage.exhaustive
            and coverage.complete_traces == coverage.total_subscriptions
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        if self.trace_coverage is not None:
            payload["trace_coverage"]["exhaustive"] = self.trace_coverage.exhaustive
            payload["trace_coverage"]["coverage_ratio"] = (
                self.trace_coverage.coverage_ratio
            )
        payload["aggregate_preconditions_ready"] = self.aggregate_preconditions_ready
        payload["customer_trace_cutover_ready"] = self.customer_trace_cutover_ready
        return payload


@dataclass(frozen=True)
class FiberTraceHop:
    """One explicitly evidenced asset in an ordered customer fiber trace."""

    kind: str
    label: str
    asset_id: object | None
    evidence: str
    validation: str = "validated"
    operational_state: str | None = None


@dataclass(frozen=True)
class FiberTraceGap:
    """A missing or conflicting edge which the resolver refuses to infer."""

    code: str
    message: str
    after_kind: str | None = None
    after_asset_id: object | None = None


@dataclass(frozen=True)
class FiberSubscriptionTrace:
    subscription_id: object
    customer_label: str
    subscription_status: str
    hops: tuple[FiberTraceHop, ...]
    gaps: tuple[FiberTraceGap, ...]
    electronic_complete: bool
    physical_complete: bool
    upstream_scope: str
    upstream_message: str

    @property
    def customer_trace_complete(self) -> bool:
        return self.electronic_complete and self.physical_complete and not self.gaps

    @property
    def first_gap(self) -> FiberTraceGap | None:
        return self.gaps[0] if self.gaps else None

    @property
    def last_validated_scope(self) -> FiberTraceHop | None:
        if not self.gaps:
            return self.hops[-1] if self.hops else None
        first_gap = self.gaps[0]
        if first_gap.after_asset_id is None:
            return None
        return next(
            (
                hop
                for hop in reversed(self.hops)
                if hop.validation == "validated"
                and hop.asset_id == first_gap.after_asset_id
                and (first_gap.after_kind is None or hop.kind == first_gap.after_kind)
            ),
            None,
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["customer_trace_complete"] = self.customer_trace_complete
        payload["first_gap"] = asdict(self.first_gap) if self.first_gap else None
        payload["last_validated_scope"] = (
            asdict(self.last_validated_scope) if self.last_validated_scope else None
        )
        return payload


@dataclass(frozen=True)
class FiberTraceSearchResult:
    subscription_id: object
    customer_label: str
    subscriber_number: str | None
    offer_name: str
    subscription_status: str
    ont_serial: str | None
    ont_status: str | None
    status_seen_at: datetime | None


@dataclass(frozen=True)
class FiberTraceCoverage:
    total_subscriptions: int
    evaluated_subscriptions: int
    complete_traces: int
    electronic_complete: int
    physical_complete: int
    gap_counts: tuple[tuple[str, int], ...]

    @property
    def exhaustive(self) -> bool:
        return self.evaluated_subscriptions == self.total_subscriptions

    @property
    def coverage_ratio(self) -> float:
        return (
            self.complete_traces / self.total_subscriptions
            if self.total_subscriptions
            else 1.0
        )


@dataclass(frozen=True)
class FiberCohortEvidence:
    scope: str
    asset_id: object
    total: int
    online: int
    offline: int
    stale: int

    @property
    def offline_ratio(self) -> float:
        return self.offline / self.total if self.total else 0.0


@dataclass(frozen=True)
class FiberFaultCandidate:
    scope: str
    label: str
    asset_ids: tuple[object, ...]
    score: int
    confidence: str
    evidence: FiberCohortEvidence
    rationale: str


@dataclass(frozen=True)
class FiberFaultLocalization:
    trace: FiberSubscriptionTrace
    telemetry_state: str
    telemetry_message: str
    candidates: tuple[FiberFaultCandidate, ...]
    evaluated_at: datetime
    freshness_minutes: int


@dataclass(frozen=True)
class _ValidatedSegmentPath:
    hops: tuple[FiberTraceHop, ...]
    segment_ids: tuple[object, ...]
    error_code: str | None = None
    error_message: str | None = None


def _enum_value(value) -> str:
    return str(getattr(value, "value", value))


def _customer_label(subscription: Subscription) -> str:
    subscriber = subscription.subscriber
    if subscriber is None:
        return str(subscription.id)
    return (
        subscriber.display_name
        or subscriber.company_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip()
        or subscriber.subscriber_number
        or str(subscriber.id)
    )


def _termination_hop(point: FiberTerminationPoint) -> FiberTraceHop:
    endpoint = _enum_value(point.endpoint_type)
    return FiberTraceHop(
        kind="termination",
        label=point.name or endpoint.replace("_", " ").title(),
        asset_id=point.id,
        evidence=(
            f"active FiberTerminationPoint ({endpoint}) with explicit ref_id "
            f"{point.ref_id}"
        ),
    )


def _validated_segment_path(
    db: Session,
    *,
    start_type: ODNEndpointType,
    start_ref_id,
    end_type: ODNEndpointType,
    end_ref_id,
    segment_kind: str,
) -> _ValidatedSegmentPath:
    """Return one unique shortest path through explicit operational edges.

    Multiple equally short paths are a review conflict, not permission to pick
    one. Geometry is required as evidence, but never creates adjacency by
    touching or proximity.
    """
    points = list(
        db.scalars(
            select(FiberTerminationPoint).where(
                FiberTerminationPoint.is_active.is_(True),
                FiberTerminationPoint.ref_id.is_not(None),
            )
        )
    )
    point_by_id = {point.id: point for point in points}
    starts = {
        point.id
        for point in points
        if point.endpoint_type == start_type and point.ref_id == start_ref_id
    }
    ends = {
        point.id
        for point in points
        if point.endpoint_type == end_type and point.ref_id == end_ref_id
    }
    if not starts:
        return _ValidatedSegmentPath(
            (),
            (),
            "fiber_start_termination_missing",
            f"No active referenced {start_type.value} termination exists.",
        )
    if not ends:
        return _ValidatedSegmentPath(
            (),
            (),
            "fiber_end_termination_missing",
            f"No active referenced {end_type.value} termination exists.",
        )

    segments = list(
        db.scalars(
            select(FiberSegment).where(
                FiberSegment.is_active.is_(True),
                FiberSegment.from_point_id.is_not(None),
                FiberSegment.to_point_id.is_not(None),
                FiberSegment.route_geom.is_not(None),
            )
        )
    )
    adjacency: dict[uuid.UUID, list[tuple[uuid.UUID, FiberSegment]]] = {}
    for segment in segments:
        from_point_id = segment.from_point_id
        to_point_id = segment.to_point_id
        if (
            from_point_id is None
            or to_point_id is None
            or from_point_id not in point_by_id
            or to_point_id not in point_by_id
        ):
            continue
        adjacency.setdefault(from_point_id, []).append((to_point_id, segment))
        adjacency.setdefault(to_point_id, []).append((from_point_id, segment))

    distance = dict.fromkeys(starts, 0)
    path_count = dict.fromkeys(starts, 1)
    predecessor: dict[uuid.UUID, tuple[uuid.UUID, FiberSegment] | None] = dict.fromkeys(
        starts
    )
    queue = deque(starts)
    while queue:
        current = queue.popleft()
        if distance[current] >= 128:
            continue
        for neighbor, segment in adjacency.get(current, ()):
            candidate_distance = distance[current] + 1
            if neighbor not in distance:
                distance[neighbor] = candidate_distance
                path_count[neighbor] = path_count[current]
                predecessor[neighbor] = (current, segment)
                queue.append(neighbor)
            elif distance[neighbor] == candidate_distance:
                path_count[neighbor] = min(
                    2, path_count[neighbor] + path_count[current]
                )

    reachable_ends = [point_id for point_id in ends if point_id in distance]
    if not reachable_ends:
        return _ValidatedSegmentPath(
            (),
            (),
            "fiber_segment_path_missing",
            "Referenced terminations exist, but no active segment path with "
            "approved geometry connects them.",
        )
    shortest = min(distance[point_id] for point_id in reachable_ends)
    shortest_ends = [
        point_id for point_id in reachable_ends if distance[point_id] == shortest
    ]
    total_paths = sum(path_count[point_id] for point_id in shortest_ends)
    if total_paths != 1:
        return _ValidatedSegmentPath(
            (),
            (),
            "fiber_segment_path_ambiguous",
            "Multiple equally short validated segment paths connect these assets; "
            "manual topology review is required.",
        )

    current = shortest_ends[0]
    transitions: list[tuple[uuid.UUID, uuid.UUID, FiberSegment]] = []
    while True:
        prior = predecessor[current]
        if prior is None:
            break
        previous, segment = prior
        transitions.append((previous, current, segment))
        current = previous
    transitions.reverse()

    hops: list[FiberTraceHop] = [_termination_hop(point_by_id[current])]
    segment_ids: list[object] = []
    for _previous, next_point_id, segment in transitions:
        segment_ids.append(segment.id)
        hops.append(
            FiberTraceHop(
                kind=segment_kind,
                label=segment.name,
                asset_id=segment.id,
                evidence=(
                    "active FiberSegment with two explicit termination IDs and "
                    "approved route geometry"
                ),
            )
        )
        hops.append(_termination_hop(point_by_id[next_point_id]))
    return _ValidatedSegmentPath(tuple(hops), tuple(segment_ids))


def trace_fiber_subscription(
    db: Session, subscription_id: object
) -> FiberSubscriptionTrace:
    """Resolve one customer path without subscriber/address/proximity fallback."""
    try:
        resolved_id = (
            subscription_id
            if isinstance(subscription_id, uuid.UUID)
            else uuid.UUID(str(subscription_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid subscription ID") from exc
    subscription = db.get(Subscription, resolved_id)
    if subscription is None:
        raise ValueError("Subscription not found")
    if subscription.offer is None or subscription.offer.access_type != AccessType.fiber:
        raise ValueError("Subscription is not a fiber service")

    hops: list[FiberTraceHop] = []
    gaps: list[FiberTraceGap] = []
    customer_label = _customer_label(subscription)

    def finish(
        *, electronic_complete: bool = False, physical_complete: bool = False
    ) -> FiberSubscriptionTrace:
        return FiberSubscriptionTrace(
            subscription_id=subscription.id,
            customer_label=customer_label,
            subscription_status=_enum_value(subscription.status),
            hops=tuple(hops),
            gaps=tuple(gaps),
            electronic_complete=electronic_complete,
            physical_complete=physical_complete,
            upstream_scope="pop_boundary_only",
            upstream_message=(
                "The trace starts at the resolved serving POP. Border/core/NAS "
                "forwarding is not claimed until an authoritative routing/access-"
                "path projection exists; LLDP adjacency alone is not forwarding truth."
            ),
        )

    assignments = list(
        db.scalars(
            select(OntAssignment).where(
                OntAssignment.subscription_id == subscription.id,
                OntAssignment.active.is_(True),
            )
        )
    )
    if not assignments:
        gaps.append(
            FiberTraceGap(
                "exact_ont_assignment_missing",
                "No active ONT assignment names this subscription. Subscriber and "
                "address matches are evidence only and were not used.",
            )
        )
        return finish()
    if len(assignments) != 1:
        gaps.append(
            FiberTraceGap(
                "exact_ont_assignment_conflict",
                "More than one active ONT assignment names this subscription; "
                "manual conflict resolution is required.",
            )
        )
        return finish()
    assignment = assignments[0]
    ont = db.get(OntUnit, assignment.ont_unit_id)
    if ont is None or not ont.is_active:
        gaps.append(
            FiberTraceGap(
                "active_ont_missing",
                "The exact assignment does not resolve to an active ONT.",
            )
        )
        return finish()
    if ont.pon_port_id is None:
        gaps.append(
            FiberTraceGap(
                "ont_pon_missing",
                "The ONT has no canonical PON port.",
                "ont",
                ont.id,
            )
        )
        return finish()
    if assignment.pon_port_id not in (None, ont.pon_port_id):
        gaps.append(
            FiberTraceGap(
                "assignment_pon_conflict",
                "The assignment PON projection disagrees with the ONT's canonical PON.",
                "ont",
                ont.id,
            )
        )
        return finish()

    pon = db.get(PonPort, ont.pon_port_id)
    if pon is None or not pon.is_active:
        gaps.append(
            FiberTraceGap(
                "active_pon_missing",
                "The ONT's canonical PON port is missing or inactive.",
                "ont",
                ont.id,
            )
        )
        return finish()
    if ont.olt_device_id != pon.olt_id:
        gaps.append(
            FiberTraceGap(
                "ont_olt_pon_conflict",
                "The ONT and its PON port name different OLTs.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    olt = db.get(OLTDevice, pon.olt_id)
    if olt is None or not olt.is_active:
        gaps.append(
            FiberTraceGap(
                "active_olt_missing",
                "The PON port's OLT is missing or inactive.",
                "pon_port",
                pon.id,
            )
        )
        return finish()

    identity = identity_for_ont_assignment(db, assignment)
    if identity is not None and identity.pop_site is not None:
        hops.append(
            FiberTraceHop(
                kind="pop",
                label=identity.pop_site.name,
                asset_id=identity.pop_site.id,
                evidence="network.identity resolved the OLT monitoring node to this POP",
            )
        )
    else:
        gaps.append(
            FiberTraceGap(
                "olt_pop_identity_missing",
                "network.identity cannot resolve the OLT through a monitoring node "
                "to a serving POP.",
            )
        )
        hops.append(
            FiberTraceHop(
                kind="gap",
                label="Serving POP unresolved",
                asset_id=None,
                evidence=gaps[-1].message,
                validation="gap",
            )
        )
    node = identity.network_device if identity is not None else None
    hops.append(
        FiberTraceHop(
            kind="olt",
            label=olt.name,
            asset_id=olt.id,
            evidence="PonPort.olt_id plus the network.identity OLT match",
            operational_state=getattr(node, "live_status", None),
        )
    )
    hops.append(
        FiberTraceHop(
            kind="pon_port",
            label=pon.name,
            asset_id=pon.id,
            evidence="OntUnit.pon_port_id and PonPort.olt_id agree",
        )
    )

    pon_links = list(
        db.scalars(
            select(PonPortSplitterLink).where(
                PonPortSplitterLink.pon_port_id == pon.id,
                PonPortSplitterLink.active.is_(True),
            )
        )
    )
    if len(pon_links) != 1:
        gaps.append(
            FiberTraceGap(
                "pon_splitter_link_missing"
                if not pon_links
                else "pon_splitter_link_conflict",
                "The PON must have exactly one active reviewed splitter-input link.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    input_port = db.get(SplitterPort, pon_links[0].splitter_port_id)
    if (
        input_port is None
        or not input_port.is_active
        or input_port.port_type != SplitterPortType.input
    ):
        gaps.append(
            FiberTraceGap(
                "splitter_input_invalid",
                "The reviewed PON edge does not resolve to an active splitter input.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    output_port = (
        db.get(SplitterPort, ont.splitter_port_id)
        if ont.splitter_port_id is not None
        else None
    )
    if (
        output_port is None
        or not output_port.is_active
        or output_port.port_type != SplitterPortType.output
    ):
        gaps.append(
            FiberTraceGap(
                "splitter_output_invalid",
                "The ONT does not name an active splitter output port.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    if output_port.splitter_id != input_port.splitter_id:
        gaps.append(
            FiberTraceGap(
                "splitter_identity_conflict",
                "The PON input and ONT output belong to different splitters.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    splitter = db.get(Splitter, input_port.splitter_id)
    if splitter is None or not splitter.is_active:
        gaps.append(
            FiberTraceGap(
                "active_splitter_missing",
                "The validated ports do not resolve to an active splitter.",
                "pon_port",
                pon.id,
            )
        )
        return finish()
    fdh = db.get(FdhCabinet, splitter.fdh_id) if splitter.fdh_id else None
    if fdh is None or not fdh.is_active:
        gaps.append(
            FiberTraceGap(
                "active_fdh_missing",
                "The splitter is not attached to an active FDH/FAT cabinet.",
                "splitter",
                splitter.id,
            )
        )
        return finish(electronic_complete=True)

    feeder_path = _validated_segment_path(
        db,
        start_type=ODNEndpointType.pon_port,
        start_ref_id=pon.id,
        end_type=ODNEndpointType.splitter_port,
        end_ref_id=input_port.id,
        segment_kind="feeder_segment",
    )
    if feeder_path.error_code:
        gaps.append(
            FiberTraceGap(
                feeder_path.error_code,
                feeder_path.error_message or "Validated feeder path is incomplete.",
                "pon_port",
                pon.id,
            )
        )
        hops.append(
            FiberTraceHop(
                kind="gap",
                label="Feeder path requires review",
                asset_id=None,
                evidence=gaps[-1].message,
                validation="gap",
            )
        )
    else:
        hops.extend(feeder_path.hops)

    hops.extend(
        (
            FiberTraceHop(
                kind="fdh",
                label=fdh.code or fdh.name,
                asset_id=fdh.id,
                evidence="Splitter.fdh_id resolves to an active reviewed cabinet",
            ),
            FiberTraceHop(
                kind="splitter",
                label=splitter.name,
                asset_id=splitter.id,
                evidence="PON input and ONT output resolve to the same active splitter",
            ),
            FiberTraceHop(
                kind="splitter_input",
                label=f"Input {input_port.port_number}",
                asset_id=input_port.id,
                evidence="active PonPortSplitterLink to an input port",
            ),
            FiberTraceHop(
                kind="splitter_output",
                label=f"Output {output_port.port_number}",
                asset_id=output_port.id,
                evidence="OntUnit.splitter_port_id to an output port",
            ),
        )
    )

    drop_path = _validated_segment_path(
        db,
        start_type=ODNEndpointType.splitter_port,
        start_ref_id=output_port.id,
        end_type=ODNEndpointType.ont,
        end_ref_id=ont.id,
        segment_kind="drop_segment",
    )
    if drop_path.error_code:
        gaps.append(
            FiberTraceGap(
                drop_path.error_code.replace("fiber_", "drop_", 1),
                drop_path.error_message or "Validated customer drop is incomplete.",
                "splitter_output",
                output_port.id,
            )
        )
        hops.append(
            FiberTraceHop(
                kind="gap",
                label="Customer drop requires review",
                asset_id=None,
                evidence=gaps[-1].message,
                validation="gap",
            )
        )
    else:
        hops.extend(drop_path.hops)

    seen_at = ont.olt_status_seen_at
    seen_at_utc = (
        seen_at.replace(tzinfo=UTC)
        if seen_at is not None and seen_at.tzinfo is None
        else seen_at
    )
    operational_state = _enum_value(ont.olt_status)
    if seen_at_utc is None or seen_at_utc < datetime.now(UTC) - timedelta(minutes=15):
        operational_state = f"stale_{operational_state}"
    hops.extend(
        (
            FiberTraceHop(
                kind="ont",
                label=ont.serial_number,
                asset_id=ont.id,
                evidence="the one active exact OntAssignment for this subscription",
                operational_state=operational_state,
            ),
            FiberTraceHop(
                kind="subscription",
                label=str(subscription.id),
                asset_id=subscription.id,
                evidence="OntAssignment.subscription_id",
            ),
            FiberTraceHop(
                kind="customer",
                label=customer_label,
                asset_id=subscription.subscriber_id,
                evidence="Subscription.subscriber_id",
            ),
        )
    )
    return finish(
        electronic_complete=True,
        physical_complete=(
            feeder_path.error_code is None and drop_path.error_code is None
        ),
    )


def search_fiber_trace_subscriptions(
    db: Session, query: str | None = None, *, limit: int = 20
) -> tuple[FiberTraceSearchResult, ...]:
    """Return a bounded operator worklist for the trace UI."""
    stmt = (
        select(
            Subscription.id,
            Subscriber.display_name,
            Subscriber.company_name,
            Subscriber.first_name,
            Subscriber.last_name,
            Subscriber.subscriber_number,
            CatalogOffer.name,
            Subscription.status,
            OntUnit.serial_number,
            OntUnit.olt_status,
            OntUnit.olt_status_seen_at,
        )
        .join(Subscriber, Subscriber.id == Subscription.subscriber_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .outerjoin(
            OntAssignment,
            (OntAssignment.subscription_id == Subscription.id)
            & OntAssignment.active.is_(True),
        )
        .outerjoin(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .where(CatalogOffer.access_type == AccessType.fiber)
    )
    cleaned = (query or "").strip()
    if cleaned:
        like = f"%{cleaned}%"
        predicates: list[ColumnElement[bool]] = [
            Subscriber.display_name.ilike(like),
            Subscriber.company_name.ilike(like),
            Subscriber.first_name.ilike(like),
            Subscriber.last_name.ilike(like),
            Subscriber.email.ilike(like),
            Subscriber.phone.ilike(like),
            Subscriber.subscriber_number.ilike(like),
            OntUnit.serial_number.ilike(like),
        ]
        try:
            predicates.append(Subscription.id == uuid.UUID(cleaned))
        except ValueError:
            pass
        stmt = stmt.where(or_(*predicates))
    else:
        stmt = stmt.where(Subscription.status == SubscriptionStatus.active)
    rows = db.execute(
        stmt.order_by(
            OntUnit.olt_status.asc().nullslast(),
            OntUnit.olt_status_seen_at.desc().nullslast(),
            Subscriber.last_name,
            Subscriber.first_name,
        ).limit(max(1, min(limit, 50)))
    ).all()
    results = []
    seen_subscriptions = set()
    for row in rows:
        if row[0] in seen_subscriptions:
            continue
        seen_subscriptions.add(row[0])
        customer = row[1] or row[2] or f"{row[3]} {row[4]}".strip()
        results.append(
            FiberTraceSearchResult(
                subscription_id=row[0],
                customer_label=customer,
                subscriber_number=row[5],
                offer_name=row[6],
                subscription_status=_enum_value(row[7]),
                ont_serial=row[8],
                ont_status=_enum_value(row[9]) if row[9] is not None else None,
                status_seen_at=row[10],
            )
        )
    return tuple(results)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _cohort_evidence(
    rows,
    *,
    scope: str,
    asset_id,
    key_index: int,
    cutoff: datetime,
) -> FiberCohortEvidence:
    selected = [row for row in rows if row[key_index] == asset_id]
    online = offline = stale = 0
    for row in selected:
        seen = _as_utc(row[2])
        if seen is None or seen < cutoff:
            stale += 1
        elif row[1] == OnuOnlineStatus.online:
            online += 1
        else:
            offline += 1
    return FiberCohortEvidence(
        scope=scope,
        asset_id=asset_id,
        total=online + offline,
        online=online,
        offline=offline,
        stale=stale,
    )


def localize_fiber_fault(
    db: Session,
    subscription_id: object,
    *,
    now: datetime | None = None,
    freshness_minutes: int = 15,
) -> FiberFaultLocalization:
    """Rank bounded candidate scopes from a validated trace and fresh OLT facts.

    The result is diagnostic evidence. It does not create an outage, change
    topology, or claim which individual passive segment has failed.
    """
    evaluated_at = _as_utc(now) if now is not None else datetime.now(UTC)
    assert evaluated_at is not None
    trace = trace_fiber_subscription(db, subscription_id)
    if trace.subscription_status != SubscriptionStatus.active.value:
        return FiberFaultLocalization(
            trace,
            "not_evaluable",
            "Fault ranking is limited to active fiber subscriptions.",
            (),
            evaluated_at,
            freshness_minutes,
        )
    assignments = list(
        db.scalars(
            select(OntAssignment).where(
                OntAssignment.subscription_id == trace.subscription_id,
                OntAssignment.active.is_(True),
            )
        )
    )
    if not trace.electronic_complete or len(assignments) != 1:
        return FiberFaultLocalization(
            trace,
            "not_evaluable",
            "Fault ranking requires one complete exact electronic path.",
            (),
            evaluated_at,
            freshness_minutes,
        )
    ont = db.get(OntUnit, assignments[0].ont_unit_id)
    if ont is None:
        return FiberFaultLocalization(
            trace,
            "not_evaluable",
            "The trace ONT no longer exists.",
            (),
            evaluated_at,
            freshness_minutes,
        )
    cutoff = evaluated_at - timedelta(minutes=freshness_minutes)
    selected_seen = _as_utc(ont.olt_status_seen_at)
    if selected_seen is None or selected_seen < cutoff:
        return FiberFaultLocalization(
            trace,
            "stale",
            "The selected ONT has no fresh OLT observation; no fault area was guessed.",
            (),
            evaluated_at,
            freshness_minutes,
        )
    if ont.olt_status == OnuOnlineStatus.online:
        return FiberFaultLocalization(
            trace,
            "online",
            "The selected ONT is online in fresh OLT telemetry; no outage area is ranked.",
            (),
            evaluated_at,
            freshness_minutes,
        )

    cohort_rows = db.execute(
        select(
            OntUnit.id,
            OntUnit.olt_status,
            OntUnit.olt_status_seen_at,
            OntUnit.olt_device_id,
            OntUnit.pon_port_id,
            SplitterPort.splitter_id,
            Splitter.fdh_id,
        )
        .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
        .join(Subscription, Subscription.id == OntAssignment.subscription_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .outerjoin(SplitterPort, SplitterPort.id == OntUnit.splitter_port_id)
        .outerjoin(Splitter, Splitter.id == SplitterPort.splitter_id)
        .where(
            OntAssignment.active.is_(True),
            Subscription.status == SubscriptionStatus.active,
            CatalogOffer.access_type == AccessType.fiber,
            OntUnit.is_active.is_(True),
            OntUnit.olt_device_id == ont.olt_device_id,
        )
        .distinct()
    ).all()
    output_port = (
        db.get(SplitterPort, ont.splitter_port_id)
        if ont.splitter_port_id is not None
        else None
    )
    splitter = (
        db.get(Splitter, output_port.splitter_id) if output_port is not None else None
    )
    olt_evidence = _cohort_evidence(
        cohort_rows,
        scope="olt",
        asset_id=ont.olt_device_id,
        key_index=3,
        cutoff=cutoff,
    )
    pon_evidence = _cohort_evidence(
        cohort_rows,
        scope="pon",
        asset_id=ont.pon_port_id,
        key_index=4,
        cutoff=cutoff,
    )
    splitter_evidence = (
        _cohort_evidence(
            cohort_rows,
            scope="splitter",
            asset_id=splitter.id,
            key_index=5,
            cutoff=cutoff,
        )
        if splitter is not None
        else None
    )
    fdh_evidence = (
        _cohort_evidence(
            cohort_rows,
            scope="fdh",
            asset_id=splitter.fdh_id,
            key_index=6,
            cutoff=cutoff,
        )
        if splitter is not None and splitter.fdh_id is not None
        else None
    )

    def confidence(score: int) -> str:
        if score >= 90:
            return "high"
        if score >= 70:
            return "medium"
        return "low"

    candidates: list[FiberFaultCandidate] = []
    fresh_pon_ids = {
        row[4]
        for row in cohort_rows
        if row[4] is not None
        and (seen := _as_utc(row[2])) is not None
        and seen >= cutoff
    }
    if (
        len(fresh_pon_ids) >= 2
        and olt_evidence.total >= 2
        and olt_evidence.offline_ratio >= 0.8
    ):
        score = 94 if olt_evidence.online == 0 else 76
        candidates.append(
            FiberFaultCandidate(
                "olt_or_upstream",
                "OLT or upstream power/transport",
                (ont.olt_device_id,),
                score,
                confidence(score),
                olt_evidence,
                "Most fresh customer ONTs on this OLT are offline. The trace does "
                "not extend past the POP, so upstream transport remains in the set.",
            )
        )

    feeder_assets = tuple(
        hop.asset_id
        for hop in trace.hops
        if hop.kind == "feeder_segment" and hop.asset_id is not None
    )
    if pon_evidence.total >= 2 and pon_evidence.offline_ratio >= 0.8:
        score = 96 if olt_evidence.online > 0 else 88
        candidates.append(
            FiberFaultCandidate(
                "pon_shared_branch",
                "PON or its validated shared passive branch",
                tuple(
                    asset
                    for asset in (
                        ont.pon_port_id,
                        *feeder_assets,
                        splitter.id if splitter is not None else None,
                    )
                    if asset
                ),
                score,
                confidence(score),
                pon_evidence,
                "The selected PON cohort is jointly offline. Explicit topology "
                "narrows the area to this PON and shared branch, but telemetry "
                "cannot select one segment without field or optical evidence.",
            )
        )
    if (
        fdh_evidence is not None
        and fdh_evidence.total >= 2
        and fdh_evidence.total < olt_evidence.total
        and fdh_evidence.offline_ratio >= 0.8
    ):
        score = 93 if olt_evidence.online > 0 else 81
        candidates.append(
            FiberFaultCandidate(
                "fdh",
                "FDH/FAT cabinet or its feed",
                (fdh_evidence.asset_id,),
                score,
                confidence(score),
                fdh_evidence,
                "Fresh offline services share the validated FDH/FAT scope while "
                "other OLT services provide comparison evidence.",
            )
        )
    if (
        splitter_evidence is not None
        and splitter_evidence.total >= 2
        and splitter_evidence.total < pon_evidence.total
        and splitter_evidence.offline_ratio >= 0.8
    ):
        score = 97 if pon_evidence.online > 0 else 90
        candidates.append(
            FiberFaultCandidate(
                "splitter_branch",
                "Splitter or downstream distribution branch",
                (splitter_evidence.asset_id,),
                score,
                confidence(score),
                splitter_evidence,
                "Multiple fresh offline services share this exact splitter. This "
                "is a candidate scope, not proof that the passive device failed.",
            )
        )

    if (
        splitter_evidence is None
        or splitter_evidence.total <= 1
        or splitter_evidence.online > 0
    ):
        drop_assets = tuple(
            hop.asset_id
            for hop in trace.hops
            if hop.kind == "drop_segment" and hop.asset_id is not None
        )
        score = 99 if pon_evidence.online > 0 else 58
        local_assets = tuple(
            asset
            for asset in (ont.id, ont.splitter_port_id, *drop_assets)
            if asset is not None
        )
        reason = _enum_value(ont.offline_reason) if ont.offline_reason else "unknown"
        candidates.append(
            FiberFaultCandidate(
                "customer_drop_or_ont",
                "Customer drop, premises power, or ONT",
                local_assets,
                score,
                confidence(score),
                pon_evidence,
                f"The selected ONT is offline (OLT reason: {reason}) while the "
                "shared cohort does not establish a branch-wide failure.",
            )
        )

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.scope))
    return FiberFaultLocalization(
        trace,
        "offline",
        "Candidates are ranked from fresh OLT observations and validated shared "
        "assets. They are not an automated outage declaration.",
        tuple(candidates),
        evaluated_at,
        freshness_minutes,
    )


def audit_fiber_trace_coverage(
    db: Session, *, limit: int | None = None
) -> FiberTraceCoverage:
    """Exhaustively evaluate the active-fiber cohort unless a limit is explicit.

    This is an operator audit, not a request-path helper. A limited run is useful
    for shadow sampling but can never satisfy the cutover gate.
    """
    subscription_ids = list(
        db.scalars(
            select(Subscription.id)
            .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
            .where(*_active_fiber_subscription_filter())
            .order_by(Subscription.id)
        )
    )
    total = len(subscription_ids)
    if limit is not None:
        subscription_ids = subscription_ids[: max(0, limit)]
    complete = electronic = physical = 0
    gap_counts: dict[str, int] = {}
    for subscription_id in subscription_ids:
        trace = trace_fiber_subscription(db, subscription_id)
        electronic += int(trace.electronic_complete)
        physical += int(trace.physical_complete)
        complete += int(trace.customer_trace_complete)
        for gap in trace.gaps:
            gap_counts[gap.code] = gap_counts.get(gap.code, 0) + 1
    return FiberTraceCoverage(
        total_subscriptions=total,
        evaluated_subscriptions=len(subscription_ids),
        complete_traces=complete,
        electronic_complete=electronic,
        physical_complete=physical,
        gap_counts=tuple(sorted(gap_counts.items())),
    )


def _count(db: Session, model, *predicates) -> int:
    stmt = select(func.count()).select_from(model)
    if predicates:
        stmt = stmt.where(*predicates)
    return int(db.scalar(stmt) or 0)


def _count_grouped_rows(db: Session, stmt) -> int:
    return int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


def _active_fiber_subscription_filter():
    return (
        Subscription.status == SubscriptionStatus.active,
        CatalogOffer.access_type == AccessType.fiber,
    )


def _electronic_integrity(db: Session) -> ElectronicPathIntegrity:
    active_fiber = (
        select(Subscription.id, Subscription.subscriber_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .where(*_active_fiber_subscription_filter())
        .subquery()
    )
    active_fiber_subscriptions = _count_grouped_rows(db, select(active_fiber.c.id))

    exact_subscription_assignments = int(
        db.scalar(
            select(func.count(func.distinct(active_fiber.c.id)))
            .select_from(active_fiber)
            .join(
                OntAssignment,
                (OntAssignment.subscription_id == active_fiber.c.id)
                & OntAssignment.active.is_(True),
            )
        )
        or 0
    )
    subscriber_fallback_assignments = int(
        db.scalar(
            select(func.count(func.distinct(active_fiber.c.id)))
            .select_from(active_fiber)
            .join(
                OntAssignment,
                (OntAssignment.subscriber_id == active_fiber.c.subscriber_id)
                & OntAssignment.active.is_(True),
            )
        )
        or 0
    )

    duplicate_subscription_groups = (
        select(OntAssignment.subscription_id)
        .where(
            OntAssignment.active.is_(True),
            OntAssignment.subscription_id.is_not(None),
        )
        .group_by(OntAssignment.subscription_id)
        .having(func.count(OntAssignment.id) > 1)
    )
    duplicate_subscriber_groups = (
        select(OntAssignment.subscriber_id)
        .where(
            OntAssignment.active.is_(True),
            OntAssignment.subscriber_id.is_not(None),
        )
        .group_by(OntAssignment.subscriber_id)
        .having(func.count(OntAssignment.id) > 1)
    )

    assignment_pon = aliased(PonPort)
    assignment_ont = aliased(OntUnit)
    output_port = aliased(SplitterPort)
    input_port = aliased(SplitterPort)

    subscriptions_traceable_to_splitter = int(
        db.scalar(
            select(func.count(func.distinct(active_fiber.c.id)))
            .select_from(active_fiber)
            .join(
                OntAssignment,
                (OntAssignment.subscription_id == active_fiber.c.id)
                & OntAssignment.active.is_(True),
            )
            .join(
                OntUnit,
                (OntUnit.id == OntAssignment.ont_unit_id) & OntUnit.is_active.is_(True),
            )
            .join(
                output_port,
                (output_port.id == OntUnit.splitter_port_id)
                & output_port.is_active.is_(True)
                & (output_port.port_type == SplitterPortType.output),
            )
            .join(
                PonPortSplitterLink,
                (PonPortSplitterLink.pon_port_id == OntUnit.pon_port_id)
                & PonPortSplitterLink.active.is_(True),
            )
            .join(
                input_port,
                (input_port.id == PonPortSplitterLink.splitter_port_id)
                & input_port.is_active.is_(True)
                & (input_port.port_type == SplitterPortType.input)
                & (input_port.splitter_id == output_port.splitter_id),
            )
        )
        or 0
    )

    active_olt_nodes = (
        select(NetworkDevice.matched_device_id, NetworkDevice.pop_site_id)
        .where(
            NetworkDevice.is_active.is_(True),
            NetworkDevice.matched_device_type == "olt",
            NetworkDevice.matched_device_id.is_not(None),
        )
        .subquery()
    )

    return ElectronicPathIntegrity(
        active_fiber_subscriptions=active_fiber_subscriptions,
        exact_subscription_assignments=exact_subscription_assignments,
        subscriber_fallback_assignments=subscriber_fallback_assignments,
        assignments_with_service_address=_count(
            db,
            OntAssignment,
            OntAssignment.active.is_(True),
            OntAssignment.service_address_id.is_not(None),
        ),
        active_onts_with_pon=_count(
            db,
            OntUnit,
            OntUnit.is_active.is_(True),
            OntUnit.pon_port_id.is_not(None),
        ),
        active_onts_with_splitter_port=_count(
            db,
            OntUnit,
            OntUnit.is_active.is_(True),
            OntUnit.splitter_port_id.is_not(None),
        ),
        subscriptions_traceable_to_splitter=subscriptions_traceable_to_splitter,
        onts_on_wrong_olt_pon=int(
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(PonPort, PonPort.id == OntUnit.pon_port_id)
                .where(
                    OntUnit.is_active.is_(True),
                    OntUnit.olt_device_id.is_distinct_from(PonPort.olt_id),
                )
            )
            or 0
        ),
        assignment_pon_disagrees_with_ont=int(
            db.scalar(
                select(func.count())
                .select_from(OntAssignment)
                .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
                .where(
                    OntAssignment.active.is_(True),
                    OntAssignment.pon_port_id.is_not(None),
                    OntUnit.pon_port_id.is_not(None),
                    OntAssignment.pon_port_id != OntUnit.pon_port_id,
                )
            )
            or 0
        ),
        assignments_on_wrong_olt_pon=int(
            db.scalar(
                select(func.count())
                .select_from(OntAssignment)
                .join(
                    assignment_ont,
                    assignment_ont.id == OntAssignment.ont_unit_id,
                )
                .join(
                    assignment_pon,
                    assignment_pon.id == OntAssignment.pon_port_id,
                )
                .where(
                    OntAssignment.active.is_(True),
                    assignment_ont.olt_device_id.is_distinct_from(
                        assignment_pon.olt_id
                    ),
                )
            )
            or 0
        ),
        assignments_to_inactive_ont=int(
            db.scalar(
                select(func.count())
                .select_from(OntAssignment)
                .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
                .where(
                    OntAssignment.active.is_(True),
                    OntUnit.is_active.is_(False),
                )
            )
            or 0
        ),
        assignments_to_inactive_pon=int(
            db.scalar(
                select(func.count())
                .select_from(OntAssignment)
                .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
                .where(
                    OntAssignment.active.is_(True),
                    PonPort.is_active.is_(False),
                )
            )
            or 0
        ),
        subscriptions_with_multiple_assignments=_count_grouped_rows(
            db, duplicate_subscription_groups
        ),
        subscribers_with_multiple_assignments=_count_grouped_rows(
            db, duplicate_subscriber_groups
        ),
        active_olts_with_monitoring_node=int(
            db.scalar(
                select(func.count(func.distinct(OLTDevice.id)))
                .select_from(OLTDevice)
                .join(
                    active_olt_nodes,
                    active_olt_nodes.c.matched_device_id == OLTDevice.id,
                )
                .where(OLTDevice.is_active.is_(True))
            )
            or 0
        ),
        active_olts_with_pop_site=int(
            db.scalar(
                select(func.count(func.distinct(OLTDevice.id)))
                .select_from(OLTDevice)
                .join(
                    active_olt_nodes,
                    active_olt_nodes.c.matched_device_id == OLTDevice.id,
                )
                .where(
                    OLTDevice.is_active.is_(True),
                    active_olt_nodes.c.pop_site_id.is_not(None),
                )
            )
            or 0
        ),
    )


def _passive_integrity(db: Session) -> PassivePlantIntegrity:
    return PassivePlantIntegrity(
        fdh_with_coordinates=_count(
            db,
            FdhCabinet,
            FdhCabinet.is_active.is_(True),
            FdhCabinet.latitude.is_not(None),
            FdhCabinet.longitude.is_not(None),
        ),
        splitters_with_fdh=_count(
            db,
            Splitter,
            Splitter.is_active.is_(True),
            Splitter.fdh_id.is_not(None),
        ),
        pon_links_to_input_port=int(
            db.scalar(
                select(func.count())
                .select_from(PonPortSplitterLink)
                .join(
                    SplitterPort,
                    SplitterPort.id == PonPortSplitterLink.splitter_port_id,
                )
                .where(
                    PonPortSplitterLink.active.is_(True),
                    SplitterPort.is_active.is_(True),
                    SplitterPort.port_type == SplitterPortType.input,
                )
            )
            or 0
        ),
        pon_links_to_non_input_port=int(
            db.scalar(
                select(func.count())
                .select_from(PonPortSplitterLink)
                .join(
                    SplitterPort,
                    SplitterPort.id == PonPortSplitterLink.splitter_port_id,
                )
                .where(
                    PonPortSplitterLink.active.is_(True),
                    SplitterPort.port_type != SplitterPortType.input,
                )
            )
            or 0
        ),
        ont_links_to_output_port=int(
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(SplitterPort, SplitterPort.id == OntUnit.splitter_port_id)
                .where(
                    OntUnit.is_active.is_(True),
                    SplitterPort.is_active.is_(True),
                    SplitterPort.port_type == SplitterPortType.output,
                )
            )
            or 0
        ),
        ont_links_to_non_output_port=int(
            db.scalar(
                select(func.count())
                .select_from(OntUnit)
                .join(SplitterPort, SplitterPort.id == OntUnit.splitter_port_id)
                .where(
                    OntUnit.is_active.is_(True),
                    SplitterPort.port_type != SplitterPortType.output,
                )
            )
            or 0
        ),
        strands_with_both_endpoints=_count(
            db,
            FiberStrand,
            FiberStrand.is_active.is_(True),
            FiberStrand.upstream_type.is_not(None),
            FiberStrand.upstream_id.is_not(None),
            FiberStrand.downstream_type.is_not(None),
            FiberStrand.downstream_id.is_not(None),
        ),
        terminations_with_asset_reference=_count(
            db,
            FiberTerminationPoint,
            FiberTerminationPoint.is_active.is_(True),
            FiberTerminationPoint.ref_id.is_not(None),
        ),
        segments_with_both_endpoints=_count(
            db,
            FiberSegment,
            FiberSegment.is_active.is_(True),
            FiberSegment.from_point_id.is_not(None),
            FiberSegment.to_point_id.is_not(None),
        ),
        segments_with_route_geometry=_count(
            db,
            FiberSegment,
            FiberSegment.is_active.is_(True),
            FiberSegment.route_geom.is_not(None),
        ),
        connected_segments_with_geometry=_count(
            db,
            FiberSegment,
            FiberSegment.is_active.is_(True),
            FiberSegment.from_point_id.is_not(None),
            FiberSegment.to_point_id.is_not(None),
            FiberSegment.route_geom.is_not(None),
        ),
    )


def _findings(
    inventory: FiberTopologyInventory,
    electronic: ElectronicPathIntegrity,
    passive: PassivePlantIntegrity,
) -> tuple[FiberTopologyFinding, ...]:
    findings: list[FiberTopologyFinding] = []

    def add(code: str, severity: str, count: int, message: str) -> None:
        if count:
            findings.append(FiberTopologyFinding(code, severity, count, message))

    add(
        "fiber_subscription_without_exact_ont",
        "blocker",
        max(
            0,
            electronic.active_fiber_subscriptions
            - electronic.exact_subscription_assignments,
        ),
        "Active fiber subscriptions must link to their ONT by subscription_id; "
        "subscriber-level fallback is not an authoritative service edge.",
    )
    add(
        "fiber_subscription_not_traceable_to_splitter",
        "blocker",
        max(
            0,
            electronic.active_fiber_subscriptions
            - electronic.subscriptions_traceable_to_splitter,
        ),
        "An active fiber subscription lacks a validated PON-to-splitter-to-ONT path.",
    )
    add(
        "active_ont_without_pon",
        "blocker",
        max(0, inventory.active_onts - electronic.active_onts_with_pon),
        "An active ONT is not attached to a canonical PON port.",
    )
    add(
        "active_ont_without_splitter_output",
        "blocker",
        max(0, inventory.active_onts - electronic.active_onts_with_splitter_port),
        "An active ONT is not attached to a validated splitter output port.",
    )
    add(
        "ont_pon_wrong_olt",
        "blocker",
        electronic.onts_on_wrong_olt_pon,
        "An ONT references a PON port owned by a different OLT.",
    )
    add(
        "assignment_pon_disagrees_with_ont",
        "blocker",
        electronic.assignment_pon_disagrees_with_ont,
        "The assignment PON projection disagrees with the ONT's canonical PON.",
    )
    add(
        "assignment_pon_wrong_olt",
        "blocker",
        electronic.assignments_on_wrong_olt_pon,
        "An active assignment points to a PON outside its ONT's OLT.",
    )
    add(
        "subscription_multiple_active_onts",
        "blocker",
        electronic.subscriptions_with_multiple_assignments,
        "A subscription has more than one active ONT assignment.",
    )
    add(
        "subscriber_multiple_active_onts",
        "warning",
        electronic.subscribers_with_multiple_assignments,
        "A subscriber has multiple active ONTs; each must be bound to a specific subscription.",
    )
    add(
        "assignment_inactive_ont",
        "blocker",
        electronic.assignments_to_inactive_ont,
        "An active assignment references an inactive ONT.",
    )
    add(
        "assignment_inactive_pon",
        "blocker",
        electronic.assignments_to_inactive_pon,
        "An active assignment references an inactive PON port.",
    )
    add(
        "pon_link_not_input_port",
        "blocker",
        passive.pon_links_to_non_input_port,
        "A PON-to-splitter edge terminates on a non-input splitter port.",
    )
    add(
        "ont_link_not_output_port",
        "blocker",
        passive.ont_links_to_non_output_port,
        "An ONT-to-splitter edge terminates on a non-output splitter port.",
    )
    add(
        "active_olt_without_pop_site",
        "blocker",
        max(0, inventory.active_olts - electronic.active_olts_with_pop_site),
        "An active OLT is not mapped through its monitoring node to a POP.",
    )
    add(
        "fdh_without_coordinates",
        "warning",
        max(0, inventory.active_fdh_cabinets - passive.fdh_with_coordinates),
        "An active FDH has no approved coordinate projection.",
    )
    add(
        "splitter_without_fdh",
        "blocker",
        max(0, inventory.active_splitters - passive.splitters_with_fdh),
        "An active splitter is not attached to an FDH/FAT asset.",
    )

    if electronic.active_fiber_subscriptions and not inventory.active_fdh_cabinets:
        add(
            "passive_plant_inventory_empty",
            "blocker",
            electronic.active_fiber_subscriptions,
            "No passive plant is loaded, so customer faults cannot be localized below PON/ONT.",
        )
    if electronic.active_fiber_subscriptions and not inventory.active_segments:
        add(
            "passive_segment_graph_empty",
            "blocker",
            electronic.active_fiber_subscriptions,
            "No connected fiber segments are loaded for physical path tracing.",
        )
    if inventory.active_segments:
        add(
            "segment_missing_connected_geometry",
            "blocker",
            inventory.active_segments - passive.connected_segments_with_geometry,
            "Every operational segment needs geometry and two referenced termination points.",
        )

    return tuple(findings)


def audit_fiber_topology(
    db: Session,
    *,
    verify_customer_traces: bool = False,
    trace_limit: int | None = None,
) -> FiberTopologyAudit:
    """Return inventory preconditions and, when requested, cohort trace proof."""
    inventory = FiberTopologyInventory(
        active_olts=_count(db, OLTDevice, OLTDevice.is_active.is_(True)),
        active_pon_ports=_count(db, PonPort, PonPort.is_active.is_(True)),
        active_onts=_count(db, OntUnit, OntUnit.is_active.is_(True)),
        active_ont_assignments=_count(
            db, OntAssignment, OntAssignment.active.is_(True)
        ),
        active_fdh_cabinets=_count(db, FdhCabinet, FdhCabinet.is_active.is_(True)),
        active_splitters=_count(db, Splitter, Splitter.is_active.is_(True)),
        active_splitter_ports=_count(
            db, SplitterPort, SplitterPort.is_active.is_(True)
        ),
        active_splitter_port_assignments=_count(
            db,
            SplitterPortAssignment,
            SplitterPortAssignment.active.is_(True),
        ),
        active_pon_splitter_links=_count(
            db, PonPortSplitterLink, PonPortSplitterLink.active.is_(True)
        ),
        active_access_points=_count(
            db, FiberAccessPoint, FiberAccessPoint.is_active.is_(True)
        ),
        active_splice_closures=_count(
            db, FiberSpliceClosure, FiberSpliceClosure.is_active.is_(True)
        ),
        splice_trays=_count(db, FiberSpliceTray),
        splices=_count(db, FiberSplice),
        active_strands=_count(db, FiberStrand, FiberStrand.is_active.is_(True)),
        active_termination_points=_count(
            db,
            FiberTerminationPoint,
            FiberTerminationPoint.is_active.is_(True),
        ),
        active_segments=_count(db, FiberSegment, FiberSegment.is_active.is_(True)),
    )
    electronic = _electronic_integrity(db)
    passive = _passive_integrity(db)
    return FiberTopologyAudit(
        inventory=inventory,
        electronic=electronic,
        passive=passive,
        findings=_findings(inventory, electronic, passive),
        trace_coverage=(
            audit_fiber_trace_coverage(db, limit=trace_limit)
            if verify_customer_traces
            else None
        ),
    )


__all__ = [
    "ElectronicPathIntegrity",
    "FiberCohortEvidence",
    "FiberFaultCandidate",
    "FiberFaultLocalization",
    "FiberSubscriptionTrace",
    "FiberTopologyAudit",
    "FiberTopologyFinding",
    "FiberTopologyInventory",
    "FiberTraceGap",
    "FiberTraceHop",
    "FiberTraceCoverage",
    "FiberTraceSearchResult",
    "PassivePlantIntegrity",
    "audit_fiber_topology",
    "audit_fiber_trace_coverage",
    "localize_fiber_fault",
    "search_fiber_trace_subscriptions",
    "trace_fiber_subscription",
]
