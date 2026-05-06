"""Authoritative IPAM ownership for ONT management addresses."""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.network import (
    IPVersion,
    IPv4Address,
    IpBlock,
    IpPool,
    MgmtIpMode,
    OLTDevice,
    OntAssignment,
    OntUnit,
)
from app.services.network.ont_desired_config import set_desired_config_values

logger = logging.getLogger(__name__)

MANAGEMENT_ALLOCATION_TYPE = "management"


@dataclass(frozen=True)
class ManagementIpAllocation:
    address: str
    subnet: str | None
    gateway: str | None
    pool_id: object | None = None
    record: IPv4Address | None = None
    reused: bool = False


def _reservation_notes_for_ont(ont_id: object) -> set[str]:
    return {f"ont:{ont_id}", f"Reserved for ONT {ont_id}"}


def _pool_networks(db: Session, pool: IpPool) -> list[ipaddress.IPv4Network]:
    blocks = list(
        db.scalars(
            select(IpBlock)
            .where(IpBlock.pool_id == pool.id)
            .where(IpBlock.is_active.is_(True))
        ).all()
    )
    cidrs = [str(block.cidr) for block in blocks] or [str(pool.cidr)]
    networks: list[ipaddress.IPv4Network] = []
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if isinstance(network, ipaddress.IPv4Network):
            networks.append(network)
    return networks


def _pool_contains(db: Session, pool: IpPool, address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return isinstance(parsed, ipaddress.IPv4Address) and any(
        parsed in network for network in _pool_networks(db, pool)
    )


def _pool_subnet(db: Session, pool: IpPool) -> str | None:
    del db
    try:
        return str(ipaddress.ip_network(str(pool.cidr), strict=False).netmask)
    except ValueError:
        return None


def _pool_gateway(pool: IpPool) -> str | None:
    gateway = str(getattr(pool, "gateway", "") or "").strip()
    return gateway or None


def _get_active_assignment(db: Session, ont: OntUnit) -> OntAssignment | None:
    for assignment in getattr(ont, "assignments", []) or []:
        if getattr(assignment, "active", False):
            return assignment
    return db.scalars(
        select(OntAssignment)
        .where(OntAssignment.ont_unit_id == ont.id)
        .where(OntAssignment.active.is_(True))
        .limit(1)
    ).first()


def _get_or_create_active_assignment(db: Session, ont: OntUnit) -> OntAssignment:
    assignment = _get_active_assignment(db, ont)
    if assignment is not None:
        return assignment
    assignment = OntAssignment(ont_unit_id=ont.id, active=True)
    db.add(assignment)
    db.flush()
    return assignment


def _set_legacy_cache(
    ont: OntUnit,
    assignment: OntAssignment | None,
    *,
    allocation: ManagementIpAllocation | None,
    mode: str,
) -> None:
    if allocation is None:
        if assignment is not None:
            assignment.mgmt_ip_address = None
            assignment.mgmt_ip_mode = MgmtIpMode.inactive
            assignment.mgmt_subnet = None
            assignment.mgmt_gateway = None
        set_desired_config_values(
            ont,
            {
                "management.ip_address": None,
                "management.ip_mode": mode,
                "management.subnet": None,
                "management.gateway": None,
            },
        )
        return

    if assignment is not None:
        assignment.mgmt_ip_address = allocation.address
        assignment.mgmt_ip_mode = MgmtIpMode.static_ip
        assignment.mgmt_subnet = allocation.subnet
        assignment.mgmt_gateway = allocation.gateway
    set_desired_config_values(
        ont,
        {
            "management.ip_address": allocation.address,
            "management.ip_mode": "static_ip",
            "management.subnet": allocation.subnet,
            "management.gateway": allocation.gateway,
        },
    )


def get_ont_management_ip_record(
    db: Session, ont: OntUnit | object
) -> IPv4Address | None:
    ont_id = getattr(ont, "id", None)
    if ont_id is None:
        return None
    return db.scalars(
        select(IPv4Address)
        .where(IPv4Address.ont_unit_id == ont_id)
        .where(IPv4Address.allocation_type == MANAGEMENT_ALLOCATION_TYPE)
        .limit(1)
    ).first()


def _find_pool_for_address(
    db: Session,
    *,
    address: str,
    ont: OntUnit,
    olt: OLTDevice | None,
    pool_id: object | None,
) -> IpPool | None:
    if pool_id:
        pool = db.get(IpPool, pool_id)
        if pool is not None and _pool_contains(db, pool, address):
            return pool
        return None

    olt_pool_id = getattr(olt, "mgmt_ip_pool_id", None) if olt is not None else None
    if olt_pool_id:
        pool = db.get(IpPool, olt_pool_id)
        if pool is not None and _pool_contains(db, pool, address):
            return pool

    olt_id = getattr(olt, "id", None) or getattr(ont, "olt_device_id", None)
    candidates = list(
        db.scalars(
            select(IpPool)
            .where(IpPool.is_active.is_(True))
            .where(IpPool.ip_version == IPVersion.ipv4)
        ).all()
    )
    scoped = [
        pool
        for pool in candidates
        if olt_id is not None
        and str(getattr(pool, "olt_device_id", "") or "") == str(olt_id)
    ]
    for pool in [*scoped, *candidates]:
        if _pool_contains(db, pool, address):
            return pool
    return None


def _candidate_is_available(
    db: Session,
    *,
    address: str,
    pool: IpPool,
    ont: OntUnit,
) -> tuple[bool, IPv4Address | None]:
    record = db.scalars(
        select(IPv4Address).where(IPv4Address.address == address).limit(1)
    ).first()
    if record is None:
        return True, None
    if record.pool_id is not None and str(record.pool_id) != str(pool.id):
        return False, record
    assignment = getattr(record, "assignment", None)
    if assignment is not None and getattr(assignment, "is_active", False):
        return False, record
    owner = getattr(record, "ont_unit_id", None)
    if owner is not None and str(owner) != str(ont.id):
        return False, record
    notes = str(getattr(record, "notes", "") or "").strip()
    if record.is_reserved and not owner and notes:
        if notes not in _reservation_notes_for_ont(ont.id):
            return False, record
    return True, record


def _used_management_ips(db: Session, pool: IpPool) -> set[str]:
    rows = db.scalars(select(IPv4Address).where(IPv4Address.pool_id == pool.id)).all()
    used = {
        str(row.address)
        for row in rows
        if row.is_reserved
        or row.ont_unit_id is not None
        or (
            getattr(row, "assignment", None) is not None
            and getattr(row.assignment, "is_active", False)
        )
    }
    gateway = _pool_gateway(pool)
    if gateway:
        used.add(gateway)
    return used


def _next_available_ip(db: Session, pool: IpPool) -> str | None:
    cached = str(getattr(pool, "next_available_ip", "") or "").strip()
    if (
        cached
        and _pool_contains(db, pool, cached)
        and cached not in _used_management_ips(db, pool)
    ):
        return cached

    used = _used_management_ips(db, pool)
    for network in _pool_networks(db, pool):
        for candidate_ip in network.hosts():
            candidate = str(candidate_ip)
            if candidate not in used:
                return candidate
    return None


def _advance_pool_cache(db: Session, pool: IpPool) -> None:
    used = _used_management_ips(db, pool)
    next_available = None
    available_count = 0
    for network in _pool_networks(db, pool):
        for candidate_ip in network.hosts():
            candidate = str(candidate_ip)
            if candidate in used:
                continue
            available_count += 1
            if next_available is None:
                next_available = candidate
    pool.next_available_ip = next_available
    pool.available_count = available_count


def release_ont_management_ip(
    db: Session,
    *,
    ont: OntUnit,
    mode: str = "inactive",
) -> list[str]:
    """Release IPAM-owned management addresses for an ONT."""
    released: list[str] = []
    assignment = _get_active_assignment(db, ont)
    records = list(
        db.scalars(
            select(IPv4Address).where(
                (IPv4Address.ont_unit_id == ont.id)
                | (IPv4Address.notes.in_(_reservation_notes_for_ont(ont.id)))
            )
        ).all()
    )
    legacy_ip = (
        str(getattr(assignment, "mgmt_ip_address", "") or "").strip()
        if assignment is not None
        else ""
    )
    if legacy_ip:
        legacy_record = db.scalars(
            select(IPv4Address).where(IPv4Address.address == legacy_ip).limit(1)
        ).first()
        if legacy_record is not None and legacy_record not in records:
            records.append(legacy_record)

    touched_pools: set[object] = set()
    for record in records:
        assignment_row = getattr(record, "assignment", None)
        if assignment_row is not None and getattr(assignment_row, "is_active", False):
            continue
        released.append(str(record.address))
        if record.pool_id is not None:
            touched_pools.add(record.pool_id)
        record.is_reserved = False
        record.notes = None
        record.ont_unit_id = None
        record.allocation_type = None

    _set_legacy_cache(ont, assignment, allocation=None, mode=mode)
    for pool_id in touched_pools:
        pool = db.get(IpPool, pool_id)
        if pool is not None:
            _advance_pool_cache(db, pool)
    db.flush()
    return released


def allocate_ont_management_ip(
    db: Session,
    *,
    ont: OntUnit,
    olt: OLTDevice | None = None,
    pool_id: object | None = None,
    requested_ip: str | None = None,
) -> ManagementIpAllocation:
    """Allocate or claim an ONT management IP through IPAM."""
    if olt is None and getattr(ont, "olt_device_id", None):
        olt = db.get(OLTDevice, ont.olt_device_id)
    selected = str(requested_ip or "").strip()

    pool = None
    if selected:
        try:
            selected = str(ipaddress.ip_address(selected))
        except ValueError as exc:
            raise ValueError("Selected management IP is invalid.") from exc
        pool = _find_pool_for_address(
            db,
            address=selected,
            ont=ont,
            olt=olt,
            pool_id=pool_id,
        )
        if pool is None:
            raise ValueError(
                "Selected management IP is not in an available IPAM pool."
            )
    else:
        effective_pool_id = pool_id or (
            getattr(olt, "mgmt_ip_pool_id", None) if olt else None
        )
        if not effective_pool_id:
            raise ValueError("No management IP pool configured for this ONT.")
        pool = db.get(IpPool, effective_pool_id)
        if pool is None or not getattr(pool, "is_active", False):
            raise ValueError("Management IP pool is not available.")

    locked_pool = db.scalars(
        select(IpPool).where(IpPool.id == pool.id).with_for_update()
    ).first()
    pool = locked_pool or pool
    if getattr(pool, "ip_version", None) not in (
        IPVersion.ipv4,
        IPVersion.ipv4.value,
    ):
        raise ValueError("Management IP pool must be IPv4.")

    assignment = _get_or_create_active_assignment(db, ont)
    existing = get_ont_management_ip_record(db, ont)
    if existing is not None and str(existing.pool_id) == str(pool.id):
        if not selected or str(existing.address) == selected:
            allocation = ManagementIpAllocation(
                address=str(existing.address),
                subnet=_pool_subnet(db, pool),
                gateway=_pool_gateway(pool),
                pool_id=pool.id,
                record=existing,
                reused=True,
            )
            _set_legacy_cache(
                ont, assignment, allocation=allocation, mode="static_ip"
            )
            db.flush()
            return allocation

    legacy_ip = (
        str(getattr(assignment, "mgmt_ip_address", "") or "").strip()
        if assignment is not None
        else ""
    )
    if not selected and legacy_ip and _pool_contains(db, pool, legacy_ip):
        ok, _legacy_record = _candidate_is_available(
            db, address=legacy_ip, pool=pool, ont=ont
        )
        if ok:
            selected = legacy_ip

    if not selected:
        selected = _next_available_ip(db, pool) or ""
        if not selected:
            raise ValueError("Management IP pool exhausted.")

    if not _pool_contains(db, pool, selected):
        raise ValueError("Selected management IP is not available in this pool.")
    ok, record = _candidate_is_available(db, address=selected, pool=pool, ont=ont)
    if not ok:
        if (
            record is not None
            and record.pool_id is not None
            and str(record.pool_id) != str(pool.id)
        ):
            raise ValueError(f"Management IP {selected} belongs to a different pool.")
        raise ValueError("Selected management IP is already allocated.")

    release_ont_management_ip(db, ont=ont, mode="static_ip")

    if record is None:
        record = IPv4Address(address=selected, pool_id=pool.id)
        db.add(record)
    record.pool_id = pool.id
    record.is_reserved = True
    record.notes = f"ont:{ont.id}"
    record.ont_unit_id = ont.id
    record.allocation_type = MANAGEMENT_ALLOCATION_TYPE

    allocation = ManagementIpAllocation(
        address=selected,
        subnet=_pool_subnet(db, pool),
        gateway=_pool_gateway(pool),
        pool_id=pool.id,
        record=record,
        reused=bool(legacy_ip and legacy_ip == selected),
    )
    _set_legacy_cache(ont, assignment, allocation=allocation, mode="static_ip")
    db.flush()
    _advance_pool_cache(db, pool)
    db.flush()
    logger.info(
        "Allocated management IP %s from pool %s to ONT %s",
        selected,
        pool.id,
        getattr(ont, "serial_number", ont.id),
    )
    return allocation


def sync_desired_management_ip_from_ipam(db: Session, *, ont: OntUnit) -> None:
    record = get_ont_management_ip_record(db, ont)
    if record is None:
        return
    pool = getattr(record, "pool", None)
    allocation = ManagementIpAllocation(
        address=str(record.address),
        subnet=_pool_subnet(db, pool) if pool is not None else None,
        gateway=_pool_gateway(pool) if pool is not None else None,
        pool_id=getattr(record, "pool_id", None),
        record=record,
    )
    _set_legacy_cache(
        ont,
        _get_active_assignment(db, ont),
        allocation=allocation,
        mode="static_ip",
    )
