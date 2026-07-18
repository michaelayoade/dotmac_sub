"""Single source of truth for customer-to-network access paths."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.forwarding_topology import ForwardingTopologyDeclaration
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services.common import coerce_uuid
from app.services.fiber_topology import localize_fiber_fault, trace_fiber_subscription
from app.services.network.fiber_plant_integrity import cable_capacity
from app.services.network.forwarding_topology import (
    project_authoritative_forwarding_graph,
)
from app.services.topology.customer_path import CustomerPath, resolve_customer_path


@dataclass(frozen=True)
class AccessPathSummary:
    subscription_id: object
    subscriber_id: object | None
    access_kind: str | None
    node_id: object | None
    node_name: str | None
    basestation_id: object | None
    basestation_name: str | None
    gap: str | None
    live_session: bool


@dataclass(frozen=True)
class FiberServicePathHop:
    domain: str
    kind: str
    asset_id: object
    label: str
    evidence_refs: tuple[str, ...]
    capacity: dict[str, object] | None = None


@dataclass(frozen=True)
class FiberServicePathGap:
    code: str
    domain: str
    message: str
    after_asset_id: object | None = None


@dataclass(frozen=True)
class FiberEndToEndPath:
    subscription_id: object
    evaluated_at: datetime
    hops: tuple[FiberServicePathHop, ...]
    gaps: tuple[FiberServicePathGap, ...]
    passive_complete: bool
    forwarding_complete: bool
    provisioning_nas_device_id: object | None
    live_nas_device_id: object | None
    live_nas_state: str
    forwarding_report_sha256: str | None
    forwarding_declaration_ids: tuple[object, ...]
    fault_telemetry_state: str
    fault_candidates: tuple[dict[str, object], ...]
    evidence_sha256: str

    @property
    def complete(self) -> bool:
        return self.passive_complete and self.forwarding_complete and not self.gaps

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["complete"] = self.complete
        payload["schema_version"] = 1
        return payload


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _fiber_subscription(db: Session, subscription: Subscription | str) -> Subscription:
    row = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if row is None:
        raise ValueError("subscription not found")
    return row


def _live_nas_id(db: Session, subscription: Subscription) -> uuid.UUID | None:
    if subscription.subscriber_id is None:
        return None
    row = db.execute(
        select(RadiusActiveSession.nas_device_id)
        .where(
            RadiusActiveSession.subscriber_id == subscription.subscriber_id,
            RadiusActiveSession.nas_device_id.is_not(None),
            or_(
                RadiusActiveSession.subscription_id == subscription.id,
                RadiusActiveSession.subscription_id.is_(None),
            ),
        )
        .order_by(
            case(
                (RadiusActiveSession.subscription_id == subscription.id, 0),
                else_=1,
            ),
            RadiusActiveSession.last_update.desc().nullslast(),
            RadiusActiveSession.session_start.desc(),
            RadiusActiveSession.id,
        )
        .limit(1)
    ).first()
    return row[0] if row is not None else None


def _exact_olt_node(
    db: Session, olt_id: object
) -> tuple[NetworkDevice | None, str | None]:
    rows = list(
        db.scalars(
            select(NetworkDevice).where(
                NetworkDevice.is_active.is_(True),
                NetworkDevice.matched_device_type == "olt",
                NetworkDevice.matched_device_id == coerce_uuid(olt_id),
            )
        ).all()
    )
    if not rows:
        return None, "olt_network_device_missing"
    if len(rows) != 1:
        return None, "olt_network_device_conflict"
    return rows[0], None


def _capacity_for_passive_hop(
    db: Session, kind: str, asset_id: object | None
) -> dict[str, object] | None:
    if asset_id is None or not kind.endswith("segment"):
        return None
    try:
        capacity = cable_capacity(db, coerce_uuid(asset_id))
    except (TypeError, ValueError):
        return None
    return {
        "available_fibers": capacity.available_fibers,
        "complete": capacity.complete,
        "damaged_fibers": capacity.damaged_fibers,
        "in_use_fibers": capacity.in_use_fibers,
        "modeled_fibers": capacity.modeled_fibers,
        "reserved_fibers": capacity.reserved_fibers,
        "retired_fibers": capacity.retired_fibers,
        "total_fibers": capacity.total_fibers,
        "unmodeled_fibers": capacity.unmodeled_fibers,
    }


def resolve_fiber_end_to_end_path(
    db: Session,
    subscription: Subscription | str,
    *,
    as_of: datetime | None = None,
    vrf_name: str = "main",
    maximum_forwarding_hops: int = 16,
) -> FiberEndToEndPath:
    """Compose exact ONT/passive plant with agreeing NAS/core forwarding.

    Provisioning NAS identity and live RADIUS NAS observation remain visibly
    separate. Missing edges become typed gaps; no observation or name fallback
    manufactures an authoritative hop.
    """

    subscription_obj = _fiber_subscription(db, subscription)
    evaluated_at = as_of or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    trace = trace_fiber_subscription(db, subscription_obj.id)
    hops: list[FiberServicePathHop] = []
    gaps = [
        FiberServicePathGap(
            code=f"passive.{gap.code}",
            domain="passive_fiber",
            message=gap.message,
            after_asset_id=gap.after_asset_id,
        )
        for gap in trace.gaps
    ]
    cable_capacity_complete = True
    for hop in reversed(trace.hops):
        if hop.asset_id is None or hop.validation != "validated":
            continue
        capacity = _capacity_for_passive_hop(db, hop.kind, hop.asset_id)
        if capacity is not None and capacity["complete"] is False:
            cable_capacity_complete = False
            gaps.append(
                FiberServicePathGap(
                    code="capacity.cable_inventory_incomplete",
                    domain="passive_capacity",
                    message=(
                        "The cable's exact numbered core inventory does not match "
                        "its declared fiber_count."
                    ),
                    after_asset_id=hop.asset_id,
                )
            )
        hops.append(
            FiberServicePathHop(
                domain="passive_fiber",
                kind=hop.kind,
                asset_id=hop.asset_id,
                label=hop.label,
                evidence_refs=(hop.evidence,),
                capacity=capacity,
            )
        )

    passive_complete = trace.customer_trace_complete and cable_capacity_complete
    olt_hops = [hop for hop in trace.hops if hop.kind == "olt" and hop.asset_id]
    olt_node: NetworkDevice | None = None
    if len(olt_hops) != 1:
        gaps.append(
            FiberServicePathGap(
                code="identity.olt_missing_or_conflicting",
                domain="network_identity",
                message="The passive trace does not name exactly one serving OLT.",
            )
        )
    else:
        olt_node, node_gap = _exact_olt_node(db, olt_hops[0].asset_id)
        if node_gap:
            gaps.append(
                FiberServicePathGap(
                    code=f"identity.{node_gap}",
                    domain="network_identity",
                    message=(
                        "network.identity does not resolve the serving OLT to one "
                        "exact active forwarding device."
                    ),
                    after_asset_id=olt_hops[0].asset_id,
                )
            )

    expected_nas_id = subscription_obj.provisioning_nas_device_id
    expected_nas = (
        db.get(NasDevice, expected_nas_id) if expected_nas_id is not None else None
    )
    nas_node_id: uuid.UUID | None = None
    if expected_nas_id is None:
        gaps.append(
            FiberServicePathGap(
                code="provisioning.nas_missing",
                domain="provisioning",
                message="The subscription has no authoritative provisioning NAS.",
            )
        )
    elif expected_nas is None or not expected_nas.is_active:
        gaps.append(
            FiberServicePathGap(
                code="provisioning.nas_inactive",
                domain="provisioning",
                message="The authoritative provisioning NAS is missing or inactive.",
                after_asset_id=expected_nas_id,
            )
        )
    elif expected_nas.network_device_id is None:
        gaps.append(
            FiberServicePathGap(
                code="identity.nas_network_device_missing",
                domain="network_identity",
                message="The provisioning NAS has no exact NetworkDevice identity.",
                after_asset_id=expected_nas.id,
            )
        )
    else:
        nas_node = db.get(NetworkDevice, expected_nas.network_device_id)
        if nas_node is None or not nas_node.is_active:
            gaps.append(
                FiberServicePathGap(
                    code="identity.nas_network_device_inactive",
                    domain="network_identity",
                    message="The provisioning NAS NetworkDevice is missing or inactive.",
                    after_asset_id=expected_nas.id,
                )
            )
        else:
            nas_node_id = nas_node.id

    live_nas_id = _live_nas_id(db, subscription_obj)
    live_nas_state = (
        "missing_observation"
        if live_nas_id is None
        else "agreement"
        if live_nas_id == expected_nas_id
        else "drift"
    )

    graph = None
    used_declaration_ids: list[uuid.UUID] = []
    forwarding_complete = False
    chain_ids: list[uuid.UUID] = []
    if olt_node is not None:
        graph = project_authoritative_forwarding_graph(db, vrf_name=vrf_name)
        hops.append(
            FiberServicePathHop(
                domain="forwarding",
                kind="access_network_device",
                asset_id=olt_node.id,
                label=olt_node.name,
                evidence_refs=(
                    f"network.identity:olt:{olt_hops[0].asset_id}",
                    f"forwarding-report:{graph.report_sha256}",
                ),
            )
        )
        seen = {olt_node.id}
        current = olt_node.id
        reached_root = current in graph.root_device_ids
        while not reached_root and len(chain_ids) < maximum_forwarding_hops:
            upstream = graph.upstream_by_downstream.get(current)
            declaration_id = graph.declaration_by_downstream.get(current)
            if upstream is None or declaration_id is None:
                break
            if upstream in seen:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.cycle",
                        domain="forwarding",
                        message="The agreeing forwarding projection contains a cycle.",
                        after_asset_id=current,
                    )
                )
                break
            device = db.get(NetworkDevice, upstream)
            if device is None or not device.is_active:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.device_missing",
                        domain="forwarding",
                        message="An agreeing forwarding hop no longer resolves active.",
                        after_asset_id=current,
                    )
                )
                break
            chain_ids.append(upstream)
            used_declaration_ids.append(declaration_id)
            hops.append(
                FiberServicePathHop(
                    domain="forwarding",
                    kind=("nas" if upstream == nas_node_id else "network_device"),
                    asset_id=upstream,
                    label=device.name,
                    evidence_refs=(f"forwarding-declaration:{declaration_id}",),
                )
            )
            seen.add(upstream)
            current = upstream
            reached_root = current in graph.root_device_ids
        if not reached_root:
            gaps.append(
                FiberServicePathGap(
                    code="forwarding.core_or_border_root_missing",
                    domain="forwarding",
                    message=(
                        "No complete observation-agreeing declaration chain reaches "
                        "an authoritative core or border root."
                    ),
                    after_asset_id=current,
                )
            )
        if nas_node_id is not None and nas_node_id not in chain_ids:
            gaps.append(
                FiberServicePathGap(
                    code="forwarding.provisioning_nas_not_on_path",
                    domain="forwarding",
                    message=(
                        "The agreeing OLT-to-root chain does not traverse the "
                        "subscription's authoritative provisioning NAS."
                    ),
                    after_asset_id=olt_node.id,
                )
            )
        nas_termination = None
        if nas_node_id is not None:
            nas_termination = db.scalar(
                select(ForwardingTopologyDeclaration).where(
                    ForwardingTopologyDeclaration.id.in_(used_declaration_ids),
                    ForwardingTopologyDeclaration.active.is_(True),
                    ForwardingTopologyDeclaration.path_kind == "nas_termination",
                    ForwardingTopologyDeclaration.downstream_device_id == nas_node_id,
                    ForwardingTopologyDeclaration.nas_device_id == expected_nas_id,
                )
            )
            if nas_termination is None:
                gaps.append(
                    FiberServicePathGap(
                        code="forwarding.nas_termination_declaration_missing",
                        domain="forwarding",
                        message=(
                            "The selected chain lacks an exact agreeing NAS "
                            "termination declaration for this provisioning NAS."
                        ),
                        after_asset_id=nas_node_id,
                    )
                )
        forwarding_complete = bool(
            reached_root
            and nas_node_id is not None
            and nas_node_id in chain_ids
            and nas_termination is not None
        )

    localization = localize_fiber_fault(db, subscription_obj.id, now=evaluated_at)
    fault_candidates = tuple(
        {
            "asset_ids": [str(value) for value in candidate.asset_ids],
            "confidence": candidate.confidence,
            "evidence": {
                "asset_id": str(candidate.evidence.asset_id),
                "offline": candidate.evidence.offline,
                "online": candidate.evidence.online,
                "scope": candidate.evidence.scope,
                "stale": candidate.evidence.stale,
                "total": candidate.evidence.total,
            },
            "label": candidate.label,
            "rationale": candidate.rationale,
            "scope": candidate.scope,
            "score": candidate.score,
        }
        for candidate in localization.candidates
    )
    evidence_payload = {
        "evaluated_at": evaluated_at.isoformat(),
        "fault_candidates": fault_candidates,
        "fault_telemetry_state": localization.telemetry_state,
        "forwarding_declaration_ids": [str(value) for value in used_declaration_ids],
        "forwarding_report_sha256": graph.report_sha256 if graph else None,
        "gaps": [asdict(gap) for gap in gaps],
        "hops": [asdict(hop) for hop in hops],
        "live_nas_device_id": str(live_nas_id) if live_nas_id else None,
        "live_nas_state": live_nas_state,
        "passive_complete": passive_complete,
        "provisioning_nas_device_id": (
            str(expected_nas_id) if expected_nas_id else None
        ),
        "subscription_id": str(subscription_obj.id),
    }
    return FiberEndToEndPath(
        subscription_id=subscription_obj.id,
        evaluated_at=evaluated_at,
        hops=tuple(hops),
        gaps=tuple(gaps),
        passive_complete=passive_complete,
        forwarding_complete=forwarding_complete,
        provisioning_nas_device_id=expected_nas_id,
        live_nas_device_id=live_nas_id,
        live_nas_state=live_nas_state,
        forwarding_report_sha256=graph.report_sha256 if graph else None,
        forwarding_declaration_ids=tuple(used_declaration_ids),
        fault_telemetry_state=localization.telemetry_state,
        fault_candidates=fault_candidates,
        evidence_sha256=_digest(evidence_payload),
    )


def resolve_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> CustomerPath:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    return resolve_customer_path(db, subscription_obj)


def summarize_subscription_access_path(
    db: Session,
    subscription: Subscription | str,
) -> AccessPathSummary:
    subscription_obj = (
        subscription
        if isinstance(subscription, Subscription)
        else db.get(Subscription, coerce_uuid(subscription))
    )
    if subscription_obj is None:
        raise ValueError("subscription not found")
    path = resolve_customer_path(db, subscription_obj)
    return AccessPathSummary(
        subscription_id=subscription_obj.id,
        subscriber_id=subscription_obj.subscriber_id,
        access_kind=path.access_device_kind,
        node_id=getattr(path.node, "id", None),
        node_name=getattr(path.node, "name", None),
        basestation_id=getattr(path.basestation, "id", None),
        basestation_name=getattr(path.basestation, "name", None),
        gap=path.gap,
        live_session=path.live_session,
    )


def resolve_subscriber_access_paths(
    db: Session,
    subscriber_id,
    *,
    active_only: bool = True,
) -> dict[object, CustomerPath]:
    stmt = select(Subscription).where(
        Subscription.subscriber_id == coerce_uuid(subscriber_id)
    )
    if active_only:
        stmt = stmt.where(Subscription.status == SubscriptionStatus.active)
    stmt = stmt.order_by(Subscription.created_at.desc())
    paths: dict[object, CustomerPath] = {}
    for subscription in db.scalars(stmt).all():
        paths[subscription.id] = resolve_customer_path(db, subscription)
    return paths


def access_path_scope(path: CustomerPath) -> dict[str, Any | None]:
    return {
        "access_kind": path.access_device_kind,
        "node_id": str(path.node.id) if path.node is not None else None,
        "node_name": getattr(path.node, "name", None),
        "basestation_id": str(path.basestation.id)
        if path.basestation is not None
        else None,
        "basestation_name": getattr(path.basestation, "name", None),
        "gap": path.gap,
        "live_session": path.live_session,
    }
