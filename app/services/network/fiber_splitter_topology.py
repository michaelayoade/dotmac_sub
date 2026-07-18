"""Read-only resolution of exact reviewed splitter cascade trees.

The canonical cascade writer lives in ``network.fiber_access_attachments``.
This module only resolves the directed PON input and splitter output/input edges;
it never infers an edge from names, ratios, cabinets, geometry, or proximity.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.fiber_access_attachment import SplitterCascadeLink
from app.models.network import (
    OLTDevice,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)

_ModelT = TypeVar("_ModelT")


class FiberSplitterTopologyError(ValueError):
    """Raised when exact splitter edges do not form one rooted tree path."""


@dataclass(frozen=True)
class SplitterChainStage:
    splitter_id: uuid.UUID
    input_port_id: uuid.UUID
    incoming_cascade_link_id: uuid.UUID | None
    upstream_output_port_id: uuid.UUID | None
    stage: int
    insertion_loss_db: Decimal | None
    cumulative_loss_db: Decimal | None


@dataclass(frozen=True)
class RootedSplitterChain:
    pon_port_id: uuid.UUID
    olt_id: uuid.UUID
    stages: tuple[SplitterChainStage, ...]

    @property
    def leaf(self) -> SplitterChainStage:
        return self.stages[-1]


@dataclass(frozen=True)
class _CascadeEdge:
    link: SplitterCascadeLink
    output_port: SplitterPort
    input_port: SplitterPort


@dataclass(frozen=True)
class _Graph:
    splitters: dict[uuid.UUID, Splitter]
    ports: dict[uuid.UUID, SplitterPort]
    outgoing: dict[uuid.UUID, tuple[_CascadeEdge, ...]]
    incoming: dict[uuid.UUID, _CascadeEdge]
    roots: dict[uuid.UUID, tuple[PonPortSplitterLink, ...]]
    invalid_splitters: frozenset[uuid.UUID]


def _all_rows(db: Session, model: type[_ModelT], *, for_update: bool) -> list[_ModelT]:
    statement: Select[tuple[_ModelT]] = select(model).order_by(
        model.id  # type: ignore[attr-defined]
    )
    if for_update:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def _load_graph(db: Session, *, for_update: bool) -> _Graph:
    splitters = {row.id: row for row in _all_rows(db, Splitter, for_update=for_update)}
    ports = {row.id: row for row in _all_rows(db, SplitterPort, for_update=for_update)}
    cascade_links = [
        row
        for row in _all_rows(db, SplitterCascadeLink, for_update=for_update)
        if row.active
    ]
    pon_links = [
        row
        for row in _all_rows(db, PonPortSplitterLink, for_update=for_update)
        if row.active
    ]

    outgoing_lists: dict[uuid.UUID, list[_CascadeEdge]] = {}
    incoming: dict[uuid.UUID, _CascadeEdge] = {}
    invalid_splitters: set[uuid.UUID] = set()
    for cascade_link in cascade_links:
        output_port = ports.get(cascade_link.upstream_output_port_id)
        input_port = ports.get(cascade_link.downstream_input_port_id)
        if output_port is None or input_port is None:
            for port in (output_port, input_port):
                if port is not None:
                    invalid_splitters.add(port.splitter_id)
            continue
        edge_splitter_ids = (output_port.splitter_id, input_port.splitter_id)
        if (
            not output_port.is_active
            or not input_port.is_active
            or output_port.port_type != SplitterPortType.output
            or input_port.port_type != SplitterPortType.input
            or output_port.splitter_id == input_port.splitter_id
            or any(
                splitter_id not in splitters or not splitters[splitter_id].is_active
                for splitter_id in edge_splitter_ids
            )
        ):
            invalid_splitters.update(edge_splitter_ids)
            continue
        edge = _CascadeEdge(cascade_link, output_port, input_port)
        if input_port.splitter_id in incoming:
            invalid_splitters.update(
                (
                    input_port.splitter_id,
                    output_port.splitter_id,
                    incoming[input_port.splitter_id].output_port.splitter_id,
                )
            )
            continue
        incoming[input_port.splitter_id] = edge
        outgoing_lists.setdefault(output_port.splitter_id, []).append(edge)

    roots_lists: dict[uuid.UUID, list[PonPortSplitterLink]] = {}
    for pon_link in pon_links:
        input_port = ports.get(pon_link.splitter_port_id)
        if (
            input_port is None
            or not input_port.is_active
            or input_port.port_type != SplitterPortType.input
        ):
            if input_port is not None:
                invalid_splitters.add(input_port.splitter_id)
            continue
        splitter = splitters.get(input_port.splitter_id)
        if splitter is None or not splitter.is_active:
            invalid_splitters.add(input_port.splitter_id)
            continue
        roots_lists.setdefault(input_port.splitter_id, []).append(pon_link)

    return _Graph(
        splitters=splitters,
        ports=ports,
        outgoing={
            splitter_id: tuple(sorted(edges, key=lambda edge: str(edge.link.id)))
            for splitter_id, edges in outgoing_lists.items()
        },
        incoming=incoming,
        roots={key: tuple(value) for key, value in roots_lists.items()},
        invalid_splitters=frozenset(invalid_splitters),
    )


def _require_single_input(splitter: Splitter) -> None:
    if splitter.input_ports != 1:
        raise FiberSplitterTopologyError(
            "cascade traversal requires explicit single-input splitter inventory"
        )


def _resolve_splitter_root_from_graph(
    db: Session,
    graph: _Graph,
    splitter_id: uuid.UUID,
) -> RootedSplitterChain:
    if splitter_id not in graph.splitters:
        raise FiberSplitterTopologyError("splitter not found")
    if splitter_id in graph.invalid_splitters:
        raise FiberSplitterTopologyError(
            "splitter participates in an invalid active source edge"
        )

    reverse_edges: list[_CascadeEdge] = []
    visited: set[uuid.UUID] = set()
    current_id = splitter_id
    while current_id in graph.incoming:
        if current_id in visited:
            raise FiberSplitterTopologyError("splitter cascade contains a cycle")
        visited.add(current_id)
        if graph.roots.get(current_id):
            raise FiberSplitterTopologyError(
                "downstream splitter cannot also have an active PON root"
            )
        edge = graph.incoming[current_id]
        reverse_edges.append(edge)
        current_id = edge.output_port.splitter_id
        if current_id in graph.invalid_splitters:
            raise FiberSplitterTopologyError(
                "splitter participates in an invalid active source edge"
            )
    if current_id in visited:
        raise FiberSplitterTopologyError("splitter cascade contains a cycle")

    root_links = graph.roots.get(current_id, ())
    if len(root_links) != 1:
        raise FiberSplitterTopologyError(
            "splitter tree must resolve to exactly one active PON root"
        )
    root_link = root_links[0]
    root_input = graph.ports[root_link.splitter_port_id]
    ordered_edges = tuple(reversed(reverse_edges))
    splitter_ids = (current_id,) + tuple(
        edge.input_port.splitter_id for edge in ordered_edges
    )
    for candidate_id in splitter_ids:
        _require_single_input(graph.splitters[candidate_id])

    losses = [
        graph.splitters[candidate_id].insertion_loss_db for candidate_id in splitter_ids
    ]
    if len(splitter_ids) > 1 and any(loss is None for loss in losses):
        raise FiberSplitterTopologyError(
            "every splitter in a cascade requires explicit insertion_loss_db"
        )

    stages: list[SplitterChainStage] = []
    cumulative: Decimal | None = (
        Decimal("0") if all(loss is not None for loss in losses) else None
    )
    for index, candidate_id in enumerate(splitter_ids):
        splitter = graph.splitters[candidate_id]
        loss = splitter.insertion_loss_db
        if cumulative is not None and loss is not None:
            cumulative += loss
        incoming_edge = ordered_edges[index - 1] if index else None
        stages.append(
            SplitterChainStage(
                splitter_id=candidate_id,
                input_port_id=(
                    root_input.id
                    if incoming_edge is None
                    else incoming_edge.input_port.id
                ),
                incoming_cascade_link_id=(
                    incoming_edge.link.id if incoming_edge else None
                ),
                upstream_output_port_id=(
                    incoming_edge.output_port.id if incoming_edge else None
                ),
                stage=index + 1,
                insertion_loss_db=loss,
                cumulative_loss_db=cumulative,
            )
        )

    pon = db.get(PonPort, root_link.pon_port_id)
    if pon is None or not pon.is_active:
        raise FiberSplitterTopologyError("splitter root PON is missing or inactive")
    olt = db.get(OLTDevice, pon.olt_id)
    if olt is None:
        raise FiberSplitterTopologyError("splitter root OLT is missing")
    return RootedSplitterChain(pon.id, pon.olt_id, tuple(stages))


def resolve_splitter_root(
    db: Session,
    splitter_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> RootedSplitterChain:
    """Resolve one splitter back to its exact active PON root."""

    return _resolve_splitter_root_from_graph(
        db,
        _load_graph(db, for_update=for_update),
        splitter_id,
    )


def resolve_splitter_chain(
    db: Session,
    pon_port_id: uuid.UUID,
    leaf_splitter_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> RootedSplitterChain:
    """Resolve the exact rooted path for one PON and leaf splitter."""

    chain = resolve_splitter_root(db, leaf_splitter_id, for_update=for_update)
    if chain.pon_port_id != pon_port_id:
        raise FiberSplitterTopologyError(
            "leaf splitter does not belong to the authoritative PON tree"
        )
    return chain


def traceable_splitter_pairs(
    db: Session,
    pairs: Iterable[tuple[uuid.UUID, uuid.UUID]],
) -> frozenset[tuple[uuid.UUID, uuid.UUID]]:
    """Resolve many PON/leaf pairs with one immutable graph snapshot."""

    graph = _load_graph(db, for_update=False)
    traceable: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for pon_port_id, leaf_splitter_id in set(pairs):
        try:
            chain = _resolve_splitter_root_from_graph(db, graph, leaf_splitter_id)
        except FiberSplitterTopologyError:
            continue
        if chain.pon_port_id == pon_port_id:
            traceable.add((pon_port_id, leaf_splitter_id))
    return frozenset(traceable)


def splitter_subtree_ids(
    db: Session,
    splitter_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> frozenset[uuid.UUID]:
    """Return exact descendants, rejecting cycles instead of guessing."""

    graph = _load_graph(db, for_update=for_update)
    if splitter_id not in graph.splitters:
        raise FiberSplitterTopologyError("splitter not found")
    visited: set[uuid.UUID] = set()
    active_path: set[uuid.UUID] = set()

    def visit(candidate_id: uuid.UUID) -> None:
        if candidate_id in active_path:
            raise FiberSplitterTopologyError("splitter cascade contains a cycle")
        if candidate_id in visited:
            return
        if candidate_id in graph.invalid_splitters:
            raise FiberSplitterTopologyError(
                "splitter participates in an invalid active source edge"
            )
        active_path.add(candidate_id)
        visited.add(candidate_id)
        for edge in graph.outgoing.get(candidate_id, ()):
            visit(edge.input_port.splitter_id)
        active_path.remove(candidate_id)

    visit(splitter_id)
    return frozenset(visited)


def lock_splitter_graph(db: Session) -> None:
    """Lock graph rows in deterministic model/ID order before a cascade write."""

    _load_graph(db, for_update=True)


__all__ = [
    "FiberSplitterTopologyError",
    "RootedSplitterChain",
    "SplitterChainStage",
    "lock_splitter_graph",
    "resolve_splitter_chain",
    "resolve_splitter_root",
    "splitter_subtree_ids",
    "traceable_splitter_pairs",
]
