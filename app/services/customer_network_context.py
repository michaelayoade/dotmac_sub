"""Shared customer network footprint queries.

This module is read-only. It centralizes how a subscriber maps to network
assets so portal, support, outage, and admin views stop rebuilding different
versions of the same ownership graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import Subscription
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    IPAssignment,
    Ipv6DelegatedPrefix,
    Ipv6PrefixState,
    OntAssignment,
    OntUnit,
)
from app.models.radius_active_session import RadiusActiveSession
from app.services.common import coerce_uuid


@dataclass(frozen=True)
class CustomerNetworkContext:
    subscriber_id: object
    ont_assignments: list[OntAssignment] = field(default_factory=list)
    cpe_devices: list[CPEDevice] = field(default_factory=list)
    active_ip_assignments: list[IPAssignment] = field(default_factory=list)
    delegated_prefixes: list[Ipv6DelegatedPrefix] = field(default_factory=list)
    active_radius_sessions: list[RadiusActiveSession] = field(default_factory=list)
    topology_paths: dict[object, object] = field(default_factory=dict)

    @property
    def has_access_equipment(self) -> bool:
        return bool(self.ont_assignments or self.cpe_devices)

    @property
    def is_online(self) -> bool:
        return bool(self.active_radius_sessions)

    @property
    def framed_ipv4_addresses(self) -> tuple[str, ...]:
        return tuple(
            session.framed_ip_address
            for session in self.active_radius_sessions
            if session.framed_ip_address
        )

    @property
    def assigned_ipv4_addresses(self) -> tuple[str, ...]:
        addresses: list[str] = []
        for assignment in self.active_ip_assignments:
            if assignment.ipv4_address is not None:
                addresses.append(assignment.ipv4_address.address)
        return tuple(addresses)

    @property
    def delegated_prefix_cidrs(self) -> tuple[str, ...]:
        return tuple(
            f"{prefix.prefix}/{prefix.prefix_length}"
            for prefix in self.delegated_prefixes
        )


def get_customer_network_context(
    db: Session,
    subscriber_id,
    *,
    include_topology_paths: bool = False,
    subscriptions: list[Subscription] | None = None,
) -> CustomerNetworkContext:
    sid = coerce_uuid(subscriber_id)
    topology_paths: dict[object, object] = {}
    if include_topology_paths:
        topology_paths = _resolve_topology_paths(db, sid, subscriptions=subscriptions)
    return CustomerNetworkContext(
        subscriber_id=sid,
        ont_assignments=list_active_ont_assignments(db, sid),
        cpe_devices=list_customer_cpe_devices(db, sid),
        active_ip_assignments=list_active_ip_assignments(db, sid),
        delegated_prefixes=list_assigned_delegated_prefixes(db, sid),
        active_radius_sessions=list_active_radius_sessions(db, sid),
        topology_paths=topology_paths,
    )


def list_active_ont_assignments(db: Session, subscriber_id) -> list[OntAssignment]:
    return (
        db.query(OntAssignment)
        .options(joinedload(OntAssignment.ont_unit))
        .filter(
            OntAssignment.subscriber_id == coerce_uuid(subscriber_id),
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.created_at.desc())
        .all()
    )


def resolve_active_customer_ont_assignment(
    db: Session, subscriber_id, *, subscription_id=None
) -> OntAssignment | None:
    """Active customer ONT assignment suitable for portal device actions."""
    if subscriber_id is None:
        return None
    query = (
        select(OntAssignment)
        .join(OntUnit, OntUnit.id == OntAssignment.ont_unit_id)
        .where(OntAssignment.subscriber_id == coerce_uuid(subscriber_id))
        .where(OntAssignment.active.is_(True))
        .where(OntUnit.is_active.is_(True))
    )
    if subscription_id is not None:
        query = query.where(
            OntAssignment.subscription_id == coerce_uuid(subscription_id)
        )
    return db.scalars(
        query.order_by(
            OntAssignment.assigned_at.desc().nullslast(), OntAssignment.id
        ).limit(1)
    ).first()


def list_customer_cpe_devices(
    db: Session,
    subscriber_id,
    *,
    active_only: bool = False,
) -> list[CPEDevice]:
    query = db.query(CPEDevice).filter(
        CPEDevice.subscriber_id == coerce_uuid(subscriber_id)
    )
    if active_only:
        query = query.filter(CPEDevice.status == DeviceStatus.active)
    return query.order_by(CPEDevice.created_at.desc()).all()


def list_active_ip_assignments(db: Session, subscriber_id) -> list[IPAssignment]:
    return (
        db.query(IPAssignment)
        .options(
            joinedload(IPAssignment.ipv4_address),
            joinedload(IPAssignment.ipv6_address),
        )
        .filter(
            IPAssignment.subscriber_id == coerce_uuid(subscriber_id),
            IPAssignment.is_active.is_(True),
        )
        .order_by(IPAssignment.created_at.desc())
        .all()
    )


def list_assigned_delegated_prefixes(
    db: Session, subscriber_id
) -> list[Ipv6DelegatedPrefix]:
    return (
        db.query(Ipv6DelegatedPrefix)
        .filter(
            Ipv6DelegatedPrefix.subscriber_id == coerce_uuid(subscriber_id),
            Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned,
        )
        .order_by(Ipv6DelegatedPrefix.prefix)
        .all()
    )


def list_active_radius_sessions(
    db: Session,
    subscriber_id,
    *,
    limit: int = 20,
) -> list[RadiusActiveSession]:
    return list(
        db.scalars(
            select(RadiusActiveSession)
            .where(RadiusActiveSession.subscriber_id == coerce_uuid(subscriber_id))
            .order_by(
                RadiusActiveSession.last_update.desc().nullslast(),
                RadiusActiveSession.session_start.desc(),
                RadiusActiveSession.id,
            )
            .limit(limit)
        ).all()
    )


def _resolve_topology_paths(
    db: Session,
    subscriber_id,
    *,
    subscriptions: list[Subscription] | None,
) -> dict[object, object]:
    from app.services.network.access_path import resolve_subscription_access_path

    subscriptions = subscriptions or list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == coerce_uuid(subscriber_id))
            .order_by(Subscription.created_at.desc())
        ).all()
    )
    paths: dict[object, object] = {}
    for subscription in subscriptions:
        try:
            paths[subscription.id] = resolve_subscription_access_path(db, subscription)
        except Exception:
            continue
    return paths
