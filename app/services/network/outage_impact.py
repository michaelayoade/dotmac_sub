"""Outage impact resolver SOT."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.network import (
    CPEDevice,
    DeviceStatus,
    FdhCabinet,
    FiberTerminationPoint,
    ODNEndpointType,
)
from app.models.network_monitoring import NetworkDevice, PopSite
from app.schemas.infrastructure import TicketLocationReference, TicketLocationSource
from app.services import infrastructure_catalogue
from app.services.bulk_actions import membership_scope_token
from app.services.common import coerce_uuid
from app.services.domain_errors import DomainError
from app.services.fiber_topology import trace_fiber_subscription
from app.services.topology.affected import affected_customers, fdh_impact_rows
from app.services.topology.customer_path import (
    RadioResolutionTier,
    resolve_customer_path,
)
from app.services.topology.ticket_asset_membership import (
    WirelessSubscriptionMatch,
    active_fiber_subscription_ids,
    wireless_subscription_match,
)

TICKET_RADIO_EDGE_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class OutageImpact:
    scope_type: str
    scope_id: object
    affected_count: int
    payload: dict

    @property
    def has_customer_impact(self) -> bool:
        return self.affected_count > 0


@dataclass(frozen=True)
class CabinetAudienceMember:
    """One affected subscription behind an FDH, with contact identity."""

    subscription_id: UUID
    subscriber_id: UUID | None
    subscriber_name: str | None
    email: str | None
    service_address: str | None


@dataclass(frozen=True)
class CabinetAudience:
    """The exact, tokenized audience behind one FDH cabinet.

    ``scope_token`` fingerprints the exact subscription-ID membership
    (order-independent SHA-256 via ``membership_scope_token``) — the evidence
    hash consumers compare between preview and execution so a send can never
    ride on a stale audience.
    """

    fdh_id: UUID
    fdh_label: str
    members: tuple[CabinetAudienceMember, ...]
    subscription_ids: tuple[UUID, ...]
    scope_token: str


class TicketAssetAudienceError(DomainError):
    """The selected asset cannot be mapped to a complete service audience."""


@dataclass(frozen=True, slots=True)
class TicketAssetAudienceQuery:
    asset: TicketLocationReference


@dataclass(frozen=True, slots=True)
class TicketAssetAudience:
    asset: TicketLocationReference
    subscription_ids: tuple[UUID, ...]
    affected_count: int
    scope_token: str


def _ticket_audience_error(code: str, message: str) -> TicketAssetAudienceError:
    return TicketAssetAudienceError(
        code=f"network.outage_impact.{code}",
        message=message,
        retryable=False,
    )


def _radio_edge_is_fresh(radio: CPEDevice) -> bool:
    synced_at = radio.uisp_synced_at
    if synced_at is None:
        return False
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return now - TICKET_RADIO_EDGE_TTL <= synced_at <= now


def resolve_ticket_asset_audience(
    db: Session, *, query: TicketAssetAudienceQuery
) -> TicketAssetAudience:
    """Prove the active service membership for one ticket's selected asset.

    Legacy subscriber-only radio links and incomplete fiber routes cannot
    authorize a bulk service pause. Fiber must pass an exhaustive active-fiber
    trace audit because a missing route could hide a member of any FAP.
    """
    asset = query.asset
    if (
        infrastructure_catalogue.resolve_ticket_location(db, reference=asset, lock=True)
        is None
    ):
        raise _ticket_audience_error(
            "asset_unavailable",
            "The selected base station or access point is unavailable.",
        )

    subscription_ids: set[UUID] = set()
    if asset.source is TicketLocationSource.base_station:
        site = db.get(PopSite, asset.id)
        if site is None:
            raise _ticket_audience_error(
                "asset_unavailable", "The selected base station is unavailable."
            )
        impact = affected_customers(db, basestation=site)
        if not impact["node_ids"]:
            raise _ticket_audience_error(
                "site_topology_missing",
                "No active network equipment is linked to this base station.",
            )
        for subscription in impact["subscriptions"]:
            path = resolve_customer_path(db, subscription)
            if (
                path.gap is not None
                or path.basestation is None
                or path.basestation.id != asset.id
                or path.radio_resolution is RadioResolutionTier.subscriber_legacy
            ):
                raise _ticket_audience_error(
                    "site_membership_unverified",
                    "A customer's route to this base station is incomplete or ambiguous.",
                )
            if path.radio is not None and not _radio_edge_is_fresh(path.radio):
                raise _ticket_audience_error(
                    "radio_topology_stale",
                    "A customer radio at this base station has not been checked recently.",
                )
            subscription_ids.add(subscription.id)
    elif asset.source is TicketLocationSource.wireless_access_point:
        radios = db.scalars(
            select(CPEDevice).where(
                CPEDevice.parent_network_device_id == asset.id,
                CPEDevice.status == DeviceStatus.active,
                or_(
                    CPEDevice.last_uisp_status.is_(None),
                    CPEDevice.last_uisp_status != "vanished",
                ),
            )
        ).all()
        for radio in radios:
            if not _radio_edge_is_fresh(radio):
                raise _ticket_audience_error(
                    "radio_topology_stale",
                    "A customer radio at this access point has not been checked recently.",
                )
            if radio.subscription_id is None:
                raise _ticket_audience_error(
                    "radio_subscription_missing",
                    "A customer radio at this access point is not linked to a specific subscription.",
                )
            match = wireless_subscription_match(
                db,
                subscription_id=radio.subscription_id,
                subscriber_id=radio.subscriber_id,
            )
            if match is WirelessSubscriptionMatch.conflicting:
                raise _ticket_audience_error(
                    "radio_subscription_conflict",
                    "A customer radio and its wireless subscription do not agree.",
                )
            if match is WirelessSubscriptionMatch.active:
                subscription_ids.add(radio.subscription_id)
    else:
        termination_ids = set(
            db.scalars(
                select(FiberTerminationPoint.id).where(
                    FiberTerminationPoint.endpoint_type
                    == ODNEndpointType.fiber_access_point,
                    FiberTerminationPoint.ref_id == asset.id,
                    FiberTerminationPoint.is_active.is_(True),
                )
            )
        )
        if len(termination_ids) != 1:
            raise _ticket_audience_error(
                "fiber_endpoint_missing",
                "This fiber access point has no single verified connection endpoint.",
            )
        for subscription_id in active_fiber_subscription_ids(db):
            trace = trace_fiber_subscription(db, subscription_id)
            if not trace.customer_trace_complete:
                raise _ticket_audience_error(
                    "fiber_route_incomplete",
                    "At least one active fiber subscription has an incomplete connection route. The full affected group cannot be verified.",
                )
            if any(
                hop.kind == "termination" and hop.asset_id in termination_ids
                for hop in trace.hops
            ):
                subscription_ids.add(subscription_id)

    if not subscription_ids:
        raise _ticket_audience_error(
            "no_active_subscriptions",
            "No active subscriptions could be verified for the selected asset.",
        )
    ordered = tuple(sorted(subscription_ids))
    return TicketAssetAudience(
        asset=asset,
        subscription_ids=ordered,
        affected_count=len(ordered),
        scope_token=membership_scope_token(
            f"ticket-asset:{asset.source.value}:{asset.id}",
            [str(subscription_id) for subscription_id in ordered],
        ),
    )


def resolve_fdh_audience(db: Session, fdh: FdhCabinet | str) -> CabinetAudience:
    """Typed audience for one FDH: exact subscriptions + membership token.

    Built on the same ``fdh_impact_rows`` resolver as the outage-impact page
    and the CRM API, so every surface sees one audience for one cabinet.
    """
    fdh_obj = (
        fdh if isinstance(fdh, FdhCabinet) else db.get(FdhCabinet, coerce_uuid(fdh))
    )
    if fdh_obj is None:
        raise ValueError("fdh cabinet not found")
    rows = fdh_impact_rows(db, fdh_obj)
    members = tuple(
        sorted(
            (
                CabinetAudienceMember(
                    subscription_id=row["subscription_id"],
                    subscriber_id=row.get("subscriber_id"),
                    subscriber_name=row.get("subscriber_name"),
                    email=(row.get("email") or "").strip() or None,
                    service_address=row.get("service_address"),
                )
                for row in rows
            ),
            key=lambda member: str(member.subscription_id),
        )
    )
    subscription_ids = tuple(member.subscription_id for member in members)
    return CabinetAudience(
        fdh_id=fdh_obj.id,
        fdh_label=fdh_obj.code or fdh_obj.name,
        members=members,
        subscription_ids=subscription_ids,
        scope_token=membership_scope_token(
            f"fdh-cabinet:{fdh_obj.id}",
            [str(subscription_id) for subscription_id in subscription_ids],
        ),
    )


def resolve_node_impact(db: Session, node: NetworkDevice | str) -> OutageImpact:
    node_obj = (
        node
        if isinstance(node, NetworkDevice)
        else db.get(NetworkDevice, coerce_uuid(node))
    )
    if node_obj is None:
        raise ValueError("network device not found")
    payload = affected_customers(db, node=node_obj)
    return OutageImpact(
        scope_type="node",
        scope_id=node_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )


def resolve_basestation_impact(db: Session, basestation: PopSite | str) -> OutageImpact:
    basestation_obj = (
        basestation
        if isinstance(basestation, PopSite)
        else db.get(PopSite, coerce_uuid(basestation))
    )
    if basestation_obj is None:
        raise ValueError("basestation not found")
    payload = affected_customers(db, basestation=basestation_obj)
    return OutageImpact(
        scope_type="basestation",
        scope_id=basestation_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )


def resolve_fdh_impact(db: Session, fdh: FdhCabinet | str) -> OutageImpact:
    fdh_obj = (
        fdh if isinstance(fdh, FdhCabinet) else db.get(FdhCabinet, coerce_uuid(fdh))
    )
    if fdh_obj is None:
        raise ValueError("fdh cabinet not found")
    payload = affected_customers(db, fdh=fdh_obj)
    return OutageImpact(
        scope_type="fdh",
        scope_id=fdh_obj.id,
        affected_count=int(payload.get("count") or 0),
        payload=payload,
    )
