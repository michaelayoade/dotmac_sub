"""Subscriber WAN static IP validation backed by IPAM assignments."""

from __future__ import annotations

import ipaddress

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    IPAssignment,
    IpBlock,
    IPv4Address,
    IPVersion,
    OntAssignment,
    OntUnit,
)


def _active_ont_assignment(db: Session, ont: OntUnit) -> OntAssignment | None:
    for assignment in getattr(ont, "assignments", []) or []:
        if getattr(assignment, "active", False):
            return assignment
    return db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()


def _active_block_containing(
    db: Session,
    address: ipaddress.IPv4Address,
) -> IpBlock | None:
    blocks = db.scalars(select(IpBlock).where(IpBlock.is_active.is_(True))).all()
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network) and address in network:
            return block
    return None


def _active_assignment_for_address(
    db: Session,
    address: IPv4Address,
) -> IPAssignment | None:
    return db.scalars(
        select(IPAssignment)
        .where(IPAssignment.ipv4_address_id == address.id)
        .where(IPAssignment.is_active.is_(True))
        .limit(1)
    ).first()


def _any_assignment_for_address(
    db: Session,
    address: IPv4Address,
) -> IPAssignment | None:
    return db.scalars(
        select(IPAssignment)
        .where(IPAssignment.ipv4_address_id == address.id)
        .limit(1)
    ).first()


def ensure_wan_static_ip_available(
    db: Session,
    *,
    ont: OntUnit,
    requested_ip: str | None,
) -> str | None:
    """Validate and claim an ONT WAN static IPv4 address when IPAM owns it.

    Addresses outside configured IPAM blocks remain manual values. Addresses
    inside IPAM blocks are represented by IPv4Address/IPAssignment so future
    subscriber allocation cannot collide with the ONT WAN static config.
    """
    ip_text = str(requested_ip or "").strip()
    if not ip_text:
        return None
    try:
        parsed = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise ValueError(f"Invalid static WAN IPv4 address: {ip_text}.") from exc
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise ValueError("Static WAN IP must be an IPv4 address.")
    normalized_ip = str(parsed)

    address = db.scalars(
        select(IPv4Address).where(IPv4Address.address == normalized_ip).limit(1)
    ).first()
    block = _active_block_containing(db, parsed)

    if address is None and block is None:
        return normalized_ip

    active_ont_assignment = _active_ont_assignment(db, ont)
    subscriber_id = getattr(active_ont_assignment, "subscriber_id", None)
    if subscriber_id is None:
        raise ValueError(
            "Static WAN IP is in IPAM space, but the ONT has no active subscriber assignment."
        )

    if address is None:
        address = IPv4Address(
            address=normalized_ip,
            pool_id=block.pool_id if block else None,
            is_reserved=False,
            allocation_type="wan",
        )
        db.add(address)
        db.flush()

    if getattr(address, "ont_unit_id", None) is not None:
        raise ValueError(
            f"Static WAN IP {normalized_ip} is already allocated as an ONT management IP."
        )
    allocation_type = str(getattr(address, "allocation_type", "") or "").strip()
    if allocation_type == "management":
        raise ValueError(
            f"Static WAN IP {normalized_ip} is already allocated as an ONT management IP."
        )
    if bool(getattr(address, "is_reserved", False)):
        raise ValueError(f"Static WAN IP {normalized_ip} is reserved in IPAM.")

    active_assignment = _active_assignment_for_address(db, address)
    if active_assignment is not None:
        if active_assignment.subscriber_id == subscriber_id:
            return normalized_ip
        raise ValueError(
            f"Static WAN IP {normalized_ip} is already assigned to another subscriber."
        )

    assignment = _any_assignment_for_address(db, address)
    if assignment is None:
        assignment = IPAssignment(
            subscriber_id=subscriber_id,
            service_address_id=getattr(active_ont_assignment, "service_address_id", None),
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
        db.add(assignment)
    else:
        assignment.subscriber_id = subscriber_id
        assignment.service_address_id = getattr(
            active_ont_assignment, "service_address_id", None
        )
        assignment.ip_version = IPVersion.ipv4
        assignment.ipv4_address_id = address.id
        assignment.ipv6_address_id = None
        assignment.is_active = True
    address.allocation_type = "wan"
    db.flush()
    return normalized_ip
