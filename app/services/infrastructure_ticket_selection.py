"""Read-only infrastructure choices for the staff ticket form.

The ticket category decides which inventory records can be selected.  This
module does not establish outage authority; the outage owner does that when a
linked ticket is issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.infrastructure import InfrastructureType
from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    NetworkDevice,
    PopSite,
)
from app.schemas.infrastructure import InfrastructureReference, InfrastructureSearch
from app.services import infrastructure_catalogue
from app.services.network.outage_impact import (
    resolve_basestation_impact,
    resolve_fdh_impact,
    resolve_node_impact,
)


class InfrastructureKind(StrEnum):
    basestation = "basestation"
    cabinet = "cabinet"
    node = "node"
    manual = "manual"


@dataclass(frozen=True, slots=True)
class InfrastructureChoice:
    kind: InfrastructureKind
    id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class InfrastructurePreview:
    choice: InfrastructureChoice
    affected_count: int | None


@dataclass(frozen=True, slots=True)
class InfrastructureTicketSearch:
    ticket_type: str
    term: str
    limit: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 20 or len(self.term) > 120:
            raise ValueError("Infrastructure search query is out of bounds")


@dataclass(frozen=True, slots=True)
class InfrastructureTicketReference:
    ticket_type: str
    kind: InfrastructureKind
    asset_id: UUID


_CATEGORY_KINDS: dict[str, tuple[InfrastructureKind, ...]] = {
    "bts outage": (InfrastructureKind.basestation,),
    "cabinet disconnection": (InfrastructureKind.cabinet,),
    "multiple cabinet disconnection": (
        InfrastructureKind.basestation,
        InfrastructureKind.node,
    ),
    "multiple cabinet link disconnection": (
        InfrastructureKind.basestation,
        InfrastructureKind.node,
    ),
    "access point outage": (InfrastructureKind.node,),
    "core link disconnection": (InfrastructureKind.manual,),
}


def catalogue_type(
    kind: InfrastructureKind, ticket_type: str
) -> InfrastructureType | None:
    if kind is InfrastructureKind.basestation:
        return InfrastructureType.base_station
    if kind is InfrastructureKind.cabinet:
        return InfrastructureType.cabinet
    if kind is InfrastructureKind.node:
        return (
            InfrastructureType.access_point
            if ticket_type.strip().casefold() == "access point outage"
            else None
        )
    raise ValueError("Manual infrastructure has no inventory reference")


def allowed_kinds(ticket_type: str | None) -> tuple[InfrastructureKind, ...]:
    return _CATEGORY_KINDS.get((ticket_type or "").strip().casefold(), ())


def category_policy() -> dict[str, tuple[str, ...]]:
    """Serialize the same category policy used by the backend for form display."""
    return {
        name: tuple(kind.value for kind in kinds)
        for name, kinds in _CATEGORY_KINDS.items()
    }


def search(
    db: Session, *, query: InfrastructureTicketSearch
) -> tuple[InfrastructureChoice, ...]:
    term = query.term.strip()
    if len(term) < 2 or not allowed_kinds(query.ticket_type):
        return ()
    results: list[InfrastructureChoice] = []
    for kind in allowed_kinds(query.ticket_type):
        if kind is InfrastructureKind.manual:
            continue
        inventory_type = catalogue_type(kind, query.ticket_type)
        options = (
            infrastructure_catalogue.search(
                db,
                query=InfrastructureSearch(
                    type=inventory_type, query=term, limit=query.limit
                ),
            )
            if inventory_type is not None
            else infrastructure_catalogue.search_network_nodes(
                db,
                query=infrastructure_catalogue.NetworkNodeSearch(
                    term=term, limit=query.limit
                ),
            )
        )
        results.extend(
            InfrastructureChoice(kind, option.id, option.label)
            for option in options.results
        )
    return tuple(results[: query.limit])


def preview(
    db: Session, *, query: InfrastructureTicketReference
) -> InfrastructurePreview | None:
    if (
        query.kind not in allowed_kinds(query.ticket_type)
        or query.kind is InfrastructureKind.manual
    ):
        return None
    inventory_type = catalogue_type(query.kind, query.ticket_type)
    option = (
        infrastructure_catalogue.resolve(
            db,
            reference=InfrastructureReference(type=inventory_type, id=query.asset_id),
        )
        if inventory_type is not None
        else infrastructure_catalogue.resolve_network_node(
            db,
            reference=infrastructure_catalogue.NetworkNodeReference(query.asset_id),
        )
    )
    if option is None:
        return None
    choice = InfrastructureChoice(query.kind, option.id, option.label)
    if query.kind is InfrastructureKind.basestation:
        basestation = db.get(PopSite, query.asset_id)
        assert basestation is not None
        return InfrastructurePreview(
            choice,
            resolve_basestation_impact(db, basestation).affected_count,
        )
    if query.kind is InfrastructureKind.cabinet:
        cabinet = db.get(FdhCabinet, query.asset_id)
        assert cabinet is not None
        return InfrastructurePreview(
            choice,
            resolve_fdh_impact(db, cabinet).affected_count,
        )
    if query.kind is InfrastructureKind.node:
        node = db.get(NetworkDevice, query.asset_id)
        assert node is not None
        return InfrastructurePreview(
            choice,
            resolve_node_impact(db, node).affected_count,
        )
    return None
