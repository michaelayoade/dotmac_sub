"""DB-backed service-port index allocator.

This module provides service-port index allocation from a DB pool,
eliminating the need for SSH queries to discover available indices.

The allocator follows the same pattern as IpPool allocation:
1. Lock pool row with SELECT FOR UPDATE
2. Try cached next_available_index
3. Verify still free, else recompute
4. Create allocation, refresh cache
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeGuard, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import (
    OLTDevice,
    OltServicePortPool,
    OntUnit,
    ServicePortAllocation,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AllocationError(Exception):
    """Raised when service-port allocation fails."""


def build_service_port_correlation_key(
    base_correlation_key: str | None,
    *,
    ont_id: UUID | str,
    vlan_id: int | None,
    gem_index: int | None,
    tag_transform: str | None = None,
    user_vlan: int | str | None = None,
) -> str | None:
    """Build an operation-scoped correlation key for one service-port mutation."""
    if not base_correlation_key:
        return None
    user_vlan_part = "" if user_vlan is None else str(user_vlan)
    tag_transform_part = tag_transform or ""
    return (
        f"{base_correlation_key}:service-port:{ont_id}:"
        f"{vlan_id}:{gem_index}:{tag_transform_part}:{user_vlan_part}"
    )


def _is_replayable_allocation(
    allocation: ServicePortAllocation | None,
) -> TypeGuard[ServicePortAllocation]:
    """Check if allocation exists and can be replayed (already provisioned with result)."""
    return (
        allocation is not None
        and allocation.provisioned_at is not None
        and isinstance(allocation.result_payload, dict)
    )


def get_or_create_pool(
    db: Session,
    olt_id: UUID | str,
    *,
    min_index: int = 0,
    max_index: int = 65535,
) -> OltServicePortPool:
    """Get existing pool or create new one for an OLT.

    Args:
        db: Database session
        olt_id: OLT device ID
        min_index: Minimum service-port index (default 0)
        max_index: Maximum service-port index (default 65535)

    Returns:
        The OltServicePortPool for this OLT
    """
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id

    stmt = select(OltServicePortPool).where(
        OltServicePortPool.olt_device_id == olt_uuid,
        OltServicePortPool.is_active.is_(True),
    )
    pool = db.scalars(stmt).first()

    if pool is None:
        # Verify OLT exists
        olt = db.get(OLTDevice, olt_uuid)
        if not olt:
            raise AllocationError(f"OLT {olt_id} not found")

        pool = OltServicePortPool(
            olt_device_id=olt_uuid,
            min_index=min_index,
            max_index=max_index,
            next_available_index=min_index,
        )
        db.add(pool)
        db.flush()
        logger.info(
            "Created service-port pool for OLT %s (range %d-%d)",
            olt.name,
            min_index,
            max_index,
        )

    return pool


def _get_allocated_indices(db: Session, pool_id: UUID) -> set[int]:
    """Get indices that cannot be reused for a pool.

    The database enforces uniqueness on ``(pool_id, port_index)`` across all
    allocation rows, not only active rows, so released rows still reserve their
    historical index.
    """
    stmt = select(ServicePortAllocation.port_index).where(
        ServicePortAllocation.pool_id == pool_id,
    )
    return set(db.scalars(stmt).all())


def _reserved_indices(pool: OltServicePortPool) -> set[int]:
    reserved: set[int] = set()
    for raw_index in pool.reserved_indices or []:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if pool.min_index <= index <= pool.max_index:
            reserved.add(index)
    return reserved


def _find_next_available(
    pool: OltServicePortPool,
    allocated: set[int],
    start_from: int | None = None,
) -> int | None:
    """Find the next available index in the pool range."""
    reserved = _reserved_indices(pool)
    start = start_from if start_from is not None else pool.min_index

    for idx in range(start, pool.max_index + 1):
        if idx not in allocated and idx not in reserved:
            return idx

    # Wrap around and check from beginning
    for idx in range(pool.min_index, start):
        if idx not in allocated and idx not in reserved:
            return idx

    return None


def _refresh_pool_cache(db: Session, pool: OltServicePortPool) -> None:
    """Update the pool's cached next_available_index and available_count."""
    allocated = _get_allocated_indices(db, pool.id)
    reserved = _reserved_indices(pool)

    total_range = pool.max_index - pool.min_index + 1
    pool.available_count = total_range - len(allocated) - len(reserved)
    pool.next_available_index = _find_next_available(pool, allocated)


def _is_available_index(
    pool: OltServicePortPool,
    index: int | None,
    allocated: set[int],
) -> bool:
    if index is None:
        return False
    return (
        pool.min_index <= index <= pool.max_index
        and index not in allocated
        and index not in _reserved_indices(pool)
    )


def allocate_service_port(
    db: Session,
    olt_id: UUID | str,
    ont_id: UUID | str,
    *,
    service_type: str | None = None,
    vlan_id: int | None = None,
    gem_index: int | None = None,
    correlation_key: str | None = None,
) -> ServicePortAllocation:
    """Allocate a service-port index for an ONT.

    This function locks the pool row and allocates the next available index.
    The allocation is recorded in the database for tracking.

    Args:
        db: Database session
        olt_id: OLT device ID
        ont_id: ONT unit ID
        service_type: Type of service (internet, management, tr069, iptv, voip)
        vlan_id: Associated VLAN ID
        gem_index: GEM port index

    Returns:
        ServicePortAllocation with the allocated port_index

    Raises:
        AllocationError: If no indices are available or allocation fails
    """
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id
    ont_uuid = UUID(str(ont_id)) if isinstance(ont_id, str) else ont_id

    if correlation_key:
        existing = find_allocation_by_correlation_key(db, correlation_key)
        if _is_replayable_allocation(existing):
            return existing

    # Lock pool row for update
    stmt = (
        select(OltServicePortPool)
        .where(
            OltServicePortPool.olt_device_id == olt_uuid,
            OltServicePortPool.is_active.is_(True),
        )
        .with_for_update()
    )
    pool = db.scalars(stmt).first()

    if not pool:
        # Create pool if it doesn't exist
        pool = get_or_create_pool(db, olt_id)
        # Re-lock after creation
        stmt = (
            select(OltServicePortPool)
            .where(OltServicePortPool.id == pool.id)
            .with_for_update()
        )
        pool = db.scalars(stmt).first()

    if not pool:
        raise AllocationError(f"Failed to acquire pool for OLT {olt_id}")

    # Verify ONT exists
    ont = db.get(OntUnit, ont_uuid)
    if not ont:
        raise AllocationError(f"ONT {ont_id} not found")

    # Get current allocations
    allocated = _get_allocated_indices(db, pool.id)

    # Try cached index first
    port_index = pool.next_available_index
    if not _is_available_index(pool, port_index, allocated):
        # Cache stale, recompute
        port_index = _find_next_available(pool, allocated)

    if port_index is None:
        # Try fresh search from beginning
        port_index = _find_next_available(pool, allocated, pool.min_index)

    if port_index is None:
        raise AllocationError(
            f"No available service-port indices on OLT (pool {pool.id})"
        )

    # Create allocation
    allocation = ServicePortAllocation(
        pool_id=pool.id,
        ont_unit_id=ont_uuid,
        port_index=port_index,
        vlan_id=vlan_id,
        gem_index=gem_index,
        service_type=service_type,
        correlation_key=correlation_key,
        is_active=True,
    )
    db.add(allocation)

    # Update cache
    allocated.add(port_index)
    pool.next_available_index = _find_next_available(pool, allocated, port_index + 1)
    reserved = _reserved_indices(pool)
    total_range = pool.max_index - pool.min_index + 1
    pool.available_count = total_range - len(allocated) - len(reserved)

    try:
        db.flush()
    except IntegrityError as exc:
        if correlation_key:
            db.rollback()
            existing = find_allocation_by_correlation_key(db, correlation_key)
            if _is_replayable_allocation(existing):
                logger.info(
                    "Reusing service-port allocation for replayed correlation key %s",
                    correlation_key,
                )
                return existing
        raise AllocationError(f"Service-port allocation failed: {exc}") from exc

    logger.info(
        "Allocated service-port %d for ONT %s on pool %s (type=%s, vlan=%s, gem=%s)",
        port_index,
        ont_id,
        pool.id,
        service_type,
        vlan_id,
        gem_index,
    )

    return allocation


def find_allocation_by_correlation_key(
    db: Session,
    correlation_key: str,
) -> ServicePortAllocation | None:
    """Find the latest allocation row for a correlation key."""
    stmt = (
        select(ServicePortAllocation)
        .where(ServicePortAllocation.correlation_key == correlation_key)
        .order_by(ServicePortAllocation.created_at.desc())
        .limit(1)
    )
    return db.scalars(stmt).first()


def release_service_port(db: Session, allocation_id: UUID | str) -> bool:
    """Release a previously allocated service-port.

    Args:
        db: Database session
        allocation_id: ID of the allocation to release

    Returns:
        True if released, False if allocation not found
    """
    alloc_uuid = (
        UUID(str(allocation_id)) if isinstance(allocation_id, str) else allocation_id
    )

    allocation = db.get(ServicePortAllocation, alloc_uuid)
    if not allocation or not allocation.is_active:
        return False

    allocation.is_active = False
    allocation.released_at = datetime.now(UTC)

    # Refresh pool cache
    pool = db.get(OltServicePortPool, allocation.pool_id)
    if pool:
        _refresh_pool_cache(db, pool)

    db.flush()

    logger.info(
        "Released service-port %d (allocation %s)",
        allocation.port_index,
        allocation_id,
    )

    return True


def release_all_for_ont(db: Session, ont_id: UUID | str) -> int:
    """Release all service-port allocations for an ONT.

    Args:
        db: Database session
        ont_id: ONT unit ID

    Returns:
        Number of allocations released
    """
    ont_uuid = UUID(str(ont_id)) if isinstance(ont_id, str) else ont_id

    stmt = select(ServicePortAllocation).where(
        ServicePortAllocation.ont_unit_id == ont_uuid,
        ServicePortAllocation.is_active.is_(True),
    )
    allocations = list(db.scalars(stmt).all())

    if not allocations:
        return 0

    now = datetime.now(UTC)
    pool_ids = set()

    for alloc in allocations:
        alloc.is_active = False
        alloc.released_at = now
        pool_ids.add(alloc.pool_id)

    # Refresh all affected pool caches
    for pool_id in pool_ids:
        pool = db.get(OltServicePortPool, pool_id)
        if pool:
            _refresh_pool_cache(db, pool)

    db.flush()

    logger.info(
        "Released %d service-port allocations for ONT %s", len(allocations), ont_id
    )

    return len(allocations)


def get_allocations_for_ont(
    db: Session,
    ont_id: UUID | str,
) -> list[ServicePortAllocation]:
    """Get all active service-port allocations for an ONT.

    Args:
        db: Database session
        ont_id: ONT unit ID

    Returns:
        List of active allocations
    """
    ont_uuid = UUID(str(ont_id)) if isinstance(ont_id, str) else ont_id

    stmt = (
        select(ServicePortAllocation)
        .where(
            ServicePortAllocation.ont_unit_id == ont_uuid,
            ServicePortAllocation.is_active.is_(True),
        )
        .order_by(ServicePortAllocation.port_index)
    )

    return list(db.scalars(stmt).all())


def find_allocation_by_index(
    db: Session,
    olt_id: UUID | str,
    port_index: int,
) -> ServicePortAllocation | None:
    """Find an active allocation by OLT and port index.

    Args:
        db: Database session
        olt_id: OLT device ID
        port_index: Service-port index

    Returns:
        The allocation if found, None otherwise
    """
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id

    # Find pool for this OLT
    pool_stmt = select(OltServicePortPool).where(
        OltServicePortPool.olt_device_id == olt_uuid,
        OltServicePortPool.is_active.is_(True),
    )
    pool = db.scalars(pool_stmt).first()
    if not pool:
        return None

    # Find allocation in this pool
    stmt = select(ServicePortAllocation).where(
        ServicePortAllocation.pool_id == pool.id,
        ServicePortAllocation.port_index == port_index,
        ServicePortAllocation.is_active.is_(True),
    )
    return db.scalars(stmt).first()


def mark_provisioned(db: Session, allocation_id: UUID | str) -> bool:
    """Mark an allocation as provisioned (written to OLT).

    Args:
        db: Database session
        allocation_id: ID of the allocation

    Returns:
        True if updated, False if not found
    """
    alloc_uuid = (
        UUID(str(allocation_id)) if isinstance(allocation_id, str) else allocation_id
    )

    allocation = db.get(ServicePortAllocation, alloc_uuid)
    if not allocation:
        return False

    allocation.provisioned_at = datetime.now(UTC)
    db.flush()

    return True


def with_allocated_service_port(
    db: Session,
    olt_id: UUID | str,
    ont_id: UUID | str,
    provision: Callable[[ServicePortAllocation], T],
    *,
    service_type: str | None = None,
    vlan_id: int | None = None,
    gem_index: int | None = None,
    correlation_key: str | None = None,
    provisioned: Callable[[T], bool] | None = None,
    serialize_result: Callable[[T], dict[str, Any] | None] | None = None,
    deserialize_result: Callable[[dict[str, Any]], T] | None = None,
) -> T:
    """Allocate an index and run the OLT write before transaction commit.

    ``allocate_service_port`` locks the pool row with ``SELECT FOR UPDATE``.
    The row lock is held until this wrapper commits or rolls back, so concurrent
    service-port creates cannot observe the same available index while the OLT
    write is still in flight.
    """
    if correlation_key and deserialize_result is not None:
        existing = find_allocation_by_correlation_key(db, correlation_key)
        if _is_replayable_allocation(existing):
            logger.info(
                "Returning cached service-port allocation result for correlation key %s",
                correlation_key,
            )
            # _is_replayable_allocation verifies result_payload is a dict
            assert isinstance(existing.result_payload, dict)
            return deserialize_result(existing.result_payload)

    allocation = allocate_service_port(
        db,
        olt_id,
        ont_id,
        service_type=service_type,
        vlan_id=vlan_id,
        gem_index=gem_index,
        correlation_key=correlation_key,
    )
    try:
        result = provision(allocation)
    except Exception:
        allocation.result_payload = None
        allocation.correlation_key = None
        release_service_port(db, allocation.id)
        db.rollback()
        raise
    is_provisioned = provisioned(result) if provisioned is not None else True
    if is_provisioned and serialize_result is not None:
        allocation.result_payload = serialize_result(result)
    if is_provisioned:
        mark_provisioned(db, allocation.id)
    else:
        allocation.result_payload = None
        allocation.correlation_key = None
        release_service_port(db, allocation.id)
    db.commit()
    return result


def sync_allocations_from_olt(
    db: Session,
    olt_id: UUID | str,
    actual_ports: list[dict],
) -> dict[str, int]:
    """Sync allocation records from OLT-observed state.

    Used during initial import or periodic reconciliation to ensure
    DB allocations match what's actually on the OLT.

    Args:
        db: Database session
        olt_id: OLT device ID
        actual_ports: List of dicts with keys: index, ont_id, vlan_id, gem_index

    Returns:
        Dict with counts: created, released, matched, orphaned
    """
    olt_uuid = UUID(str(olt_id)) if isinstance(olt_id, str) else olt_id

    # Get or create pool
    pool = get_or_create_pool(db, olt_uuid)

    # Get existing allocations
    stmt = select(ServicePortAllocation).where(
        ServicePortAllocation.pool_id == pool.id,
        ServicePortAllocation.is_active.is_(True),
    )
    existing = {a.port_index: a for a in db.scalars(stmt).all()}

    # Track what we found on OLT
    actual_indices = {p["index"] for p in actual_ports}

    created = 0
    matched = 0
    orphaned = 0

    # Process actual ports
    for port_info in actual_ports:
        idx = port_info["index"]
        if idx in existing:
            matched += 1
        else:
            # Port exists on OLT but not in DB - create placeholder
            # Note: We may not know the ont_unit_id, so this needs manual resolution
            logger.warning(
                "Service-port %d exists on OLT %s but not in allocation DB",
                idx,
                olt_id,
            )
            orphaned += 1

    # Mark allocations that don't exist on OLT as released
    released = 0
    now = datetime.now(UTC)
    for idx, alloc in existing.items():
        if idx not in actual_indices:
            alloc.is_active = False
            alloc.released_at = now
            released += 1
            logger.info(
                "Releasing stale allocation %d (not found on OLT)",
                idx,
            )

    # Refresh pool cache
    _refresh_pool_cache(db, pool)
    db.flush()

    return {
        "created": created,
        "released": released,
        "matched": matched,
        "orphaned": orphaned,
    }


def reserve_indices(
    db: Session,
    olt_id: UUID | str,
    indices: list[int],
) -> bool:
    """Reserve specific indices so they won't be allocated.

    Args:
        db: Database session
        olt_id: OLT device ID
        indices: List of indices to reserve

    Returns:
        True if updated
    """
    pool = get_or_create_pool(db, olt_id)

    existing = set(pool.reserved_indices or [])
    existing.update(indices)
    pool.reserved_indices = sorted(existing)

    _refresh_pool_cache(db, pool)
    db.flush()

    logger.info(
        "Reserved indices %s on pool %s (total reserved: %d)",
        indices,
        pool.id,
        len(pool.reserved_indices),
    )

    return True
