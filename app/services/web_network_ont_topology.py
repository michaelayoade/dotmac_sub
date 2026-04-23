"""ONT fiber path topology service for web views.

Builds the complete fiber path from OLT to ONT for visualization:
OLT → PON Port → (Feeder) → Splitter/FDH → (Drop) → ONT
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
)

logger = logging.getLogger(__name__)


@dataclass
class TopologyNode:
    """A node in the fiber path topology."""

    node_type: str  # 'olt', 'pon_port', 'splitter', 'fdh', 'ont'
    id: str | None = None
    name: str | None = None
    label: str | None = None
    status: str = "unknown"  # 'online', 'offline', 'unknown'
    url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TopologyLink:
    """A link between topology nodes."""

    link_type: str  # 'feeder', 'drop', 'pon'
    from_node: str
    to_node: str
    label: str | None = None
    distance_m: int | None = None
    loss_db: float | None = None


@dataclass
class FiberPathTopology:
    """Complete fiber path topology for an ONT."""

    nodes: list[TopologyNode] = field(default_factory=list)
    links: list[TopologyLink] = field(default_factory=list)
    available: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "nodes": [
                {
                    "node_type": n.node_type,
                    "id": n.id,
                    "name": n.name,
                    "label": n.label,
                    "status": n.status,
                    "url": n.url,
                    "details": n.details,
                }
                for n in self.nodes
            ],
            "links": [
                {
                    "link_type": l.link_type,
                    "from_node": l.from_node,
                    "to_node": l.to_node,
                    "label": l.label,
                    "distance_m": l.distance_m,
                    "loss_db": l.loss_db,
                }
                for l in self.links
            ],
            "available": self.available,
            "error": self.error,
        }


def build_ont_fiber_path(db: Session, ont_id: str) -> FiberPathTopology:
    """Build the complete fiber path topology for an ONT.

    Traces the path from OLT through PON port, splitter, and FDH cabinet to the ONT.

    Args:
        db: Database session.
        ont_id: ONT unit ID.

    Returns:
        FiberPathTopology with nodes and links representing the fiber path.
    """
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return FiberPathTopology(
            available=False,
            error="ONT not found",
        )

    nodes: list[TopologyNode] = []
    links: list[TopologyLink] = []

    # Start with the ONT node
    ont_status = "unknown"
    if hasattr(ont, "online_status") and ont.online_status:
        ont_status = (
            ont.online_status.value
            if hasattr(ont.online_status, "value")
            else str(ont.online_status)
        )

    ont_node = TopologyNode(
        node_type="ont",
        id=str(ont.id),
        name=ont.serial_number or "ONT",
        label=ont.name or ont.serial_number or "ONT",
        status=ont_status,
        url=f"/admin/network/onts/{ont.id}",
        details={
            "serial_number": ont.serial_number,
            "model": ont.model,
            "vendor": ont.vendor,
            "firmware": ont.firmware_version,
            "distance_m": ont.distance_meters if hasattr(ont, "distance_meters") else None,
        },
    )
    nodes.append(ont_node)

    # Find OLT via assignment or direct reference
    olt: OLTDevice | None = None
    pon_port: PonPort | None = None

    # Try via active assignment first
    assignment = db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id, OntAssignment.active.is_(True))
        .limit(1)
    ).first()

    if assignment and assignment.pon_port_id:
        pon_port = db.get(PonPort, assignment.pon_port_id)
        if pon_port and pon_port.olt_id:
            olt = db.get(OLTDevice, pon_port.olt_id)

    # Fallback to direct OLT reference
    if not olt and ont.olt_device_id:
        olt = db.get(OLTDevice, ont.olt_device_id)

    # Try to find PON port from board/port if not found via assignment
    if olt and not pon_port:
        fsp = f"0/{ont.board or '0'}/{ont.port or '0'}"
        pon_port = db.scalars(
            select(PonPort).where(PonPort.olt_id == olt.id, PonPort.name == fsp).limit(1)
        ).first()

    # Add OLT node
    if olt:
        olt_status = "unknown"
        if hasattr(olt, "status") and olt.status:
            olt_status = (
                olt.status.value if hasattr(olt.status, "value") else str(olt.status)
            )

        olt_node = TopologyNode(
            node_type="olt",
            id=str(olt.id),
            name=olt.name or "OLT",
            label=olt.name or "OLT",
            status=olt_status,
            url=f"/admin/network/olts/{olt.id}",
            details={
                "vendor": olt.vendor,
                "model": olt.model,
                "mgmt_ip": str(olt.mgmt_ip) if olt.mgmt_ip else None,
            },
        )
        nodes.insert(0, olt_node)

    # Add PON port node
    if pon_port:
        pon_node = TopologyNode(
            node_type="pon_port",
            id=str(pon_port.id),
            name=pon_port.name or f"PON {pon_port.port_number}",
            label=pon_port.name or f"Port {pon_port.port_number}",
            status="online" if pon_port.is_active else "offline",
            url=f"/admin/network/olts/{olt.id}?tab=pon-ports" if olt else None,
            details={
                "port_number": pon_port.port_number,
            },
        )
        # Insert after OLT
        insert_idx = 1 if olt else 0
        nodes.insert(insert_idx, pon_node)

        # Link OLT -> PON Port
        if olt:
            links.append(
                TopologyLink(
                    link_type="pon",
                    from_node=str(olt.id),
                    to_node=str(pon_port.id),
                    label="PON",
                )
            )

    # Find splitter via PON port link or ONT direct reference
    splitter: Splitter | None = None
    splitter_port: SplitterPort | None = None

    # Try PON port -> Splitter link
    if pon_port:
        pon_splitter_link = db.scalars(
            select(PonPortSplitterLink)
            .where(PonPortSplitterLink.pon_port_id == pon_port.id)
            .limit(1)
        ).first()
        if pon_splitter_link and pon_splitter_link.splitter_port_id:
            splitter_port = db.get(SplitterPort, pon_splitter_link.splitter_port_id)
            if splitter_port:
                splitter = db.get(Splitter, splitter_port.splitter_id)

    # Fallback to ONT direct splitter reference
    if not splitter and ont.splitter_id:
        splitter = db.get(Splitter, ont.splitter_id)

    if not splitter_port and ont.splitter_port_id:
        splitter_port = db.get(SplitterPort, ont.splitter_port_id)
        if splitter_port and not splitter:
            splitter = db.get(Splitter, splitter_port.splitter_id)

    # Add FDH Cabinet node (if splitter is in one)
    fdh: FdhCabinet | None = None
    if splitter and splitter.fdh_id:
        fdh = db.get(FdhCabinet, splitter.fdh_id)

    if fdh:
        fdh_node = TopologyNode(
            node_type="fdh",
            id=str(fdh.id),
            name=fdh.code or fdh.name or "FDH",
            label=fdh.name or fdh.code or "FDH Cabinet",
            status="online" if fdh.is_active else "offline",
            url=f"/admin/network/fdh-cabinets/{fdh.id}",
            details={
                "code": fdh.code,
                "notes": fdh.notes,
            },
        )
        # Insert after PON port
        fdh_insert_idx = 2 if olt and pon_port else (1 if pon_port or olt else 0)
        nodes.insert(fdh_insert_idx, fdh_node)

        # Link PON Port -> FDH (feeder)
        if pon_port:
            links.append(
                TopologyLink(
                    link_type="feeder",
                    from_node=str(pon_port.id),
                    to_node=str(fdh.id),
                    label="Feeder",
                )
            )

    # Add Splitter node
    if splitter:
        splitter_label = splitter.name or f"1:{splitter.splitter_ratio}" if splitter.splitter_ratio else "Splitter"
        splitter_node = TopologyNode(
            node_type="splitter",
            id=str(splitter.id),
            name=splitter.name or f"Splitter 1:{splitter.splitter_ratio}",
            label=splitter_label,
            status="online" if splitter.is_active else "offline",
            url=f"/admin/network/splitters/{splitter.id}",
            details={
                "ratio": f"1:{splitter.splitter_ratio}" if splitter.splitter_ratio else None,
                "port": splitter_port.port_number if splitter_port else None,
            },
        )
        # Insert before ONT (at end - 1)
        nodes.insert(len(nodes) - 1, splitter_node)

        # Link FDH -> Splitter or PON -> Splitter
        if fdh:
            links.append(
                TopologyLink(
                    link_type="internal",
                    from_node=str(fdh.id),
                    to_node=str(splitter.id),
                    label="",
                )
            )
        elif pon_port:
            links.append(
                TopologyLink(
                    link_type="feeder",
                    from_node=str(pon_port.id),
                    to_node=str(splitter.id),
                    label="Feeder",
                )
            )

        # Link Splitter -> ONT (drop)
        distance = getattr(ont, "distance_meters", None)
        links.append(
            TopologyLink(
                link_type="drop",
                from_node=str(splitter.id),
                to_node=str(ont.id),
                label=f"{distance}m" if distance else "Drop",
                distance_m=distance,
            )
        )
    elif pon_port:
        # Direct PON -> ONT link (no splitter in topology)
        distance = getattr(ont, "distance_meters", None)
        links.append(
            TopologyLink(
                link_type="drop",
                from_node=str(pon_port.id),
                to_node=str(ont.id),
                label=f"{distance}m" if distance else "Direct",
                distance_m=distance,
            )
        )
    elif olt:
        # Only OLT known
        links.append(
            TopologyLink(
                link_type="fiber",
                from_node=str(olt.id),
                to_node=str(ont.id),
                label="Fiber",
            )
        )

    return FiberPathTopology(
        nodes=nodes,
        links=links,
        available=True,
    )


def topology_tab_data(db: Session, ont_id: str) -> dict[str, Any]:
    """Build context for the Topology tab partial template.

    Args:
        db: Database session.
        ont_id: ONT unit ID.

    Returns:
        Template context dict with topology data.
    """
    topology = build_ont_fiber_path(db, ont_id)
    return {
        "topology": topology.to_dict(),
        "ont_id": ont_id,
    }
