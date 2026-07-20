"""Outage impact resolver SOT."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.network import FdhCabinet
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.common import coerce_uuid
from app.services.topology.affected import affected_customers


@dataclass(frozen=True)
class OutageImpact:
    scope_type: str
    scope_id: object
    affected_count: int
    payload: dict

    @property
    def has_customer_impact(self) -> bool:
        return self.affected_count > 0


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
