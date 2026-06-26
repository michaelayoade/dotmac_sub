"""IPv6 prefix-delegation (PD) allocator.

A real allocator service — not a one-off — for handing each subscriber a delegated
IPv6 prefix carved from a pool's parent prefix:

- **pool hierarchy**: ``IpPool`` (ip_version=ipv6) holds the parent prefix
  (``cidr``) and the per-customer delegation size (``delegation_prefix_length``,
  default /64).
- **reservation states**: each ``Ipv6DelegatedPrefix`` row is available / reserved
  / assigned; releasing returns it to available for reuse.
- **conflict checks**: prefixes are carved aligned (never overlap) and the
  ``(pool, prefix)`` unique constraint + a per-create SAVEPOINT prevent dupes.
- **concurrency**: a per-pool transaction-level advisory lock serialises
  materialisation, and ``SELECT … FOR UPDATE SKIP LOCKED`` stops two workers
  grabbing the same free row. The app is the source of truth (not FreeRADIUS
  dynamic pools).

Allocation/release flush but do not commit; the caller owns the transaction.
"""

from __future__ import annotations

import ipaddress
import itertools
import os

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.network import IpPool, Ipv6DelegatedPrefix, Ipv6PrefixState

DEFAULT_DELEGATION_PREFIX_LENGTH = 64


def pd_enabled() -> bool:
    """Feature flag (default OFF) gating IPv6 PD RADIUS emission + provisioning.
    Inert until IPv6 is actually turned on for the fleet."""
    return os.getenv("IPV6_PD_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Bound the scan for the next free prefix so a huge parent (e.g. a /32 carved
# into /64s) can't pull an unbounded candidate set.
_MATERIALIZE_SCAN_CAP = 8192

# Namespace for the per-pool advisory lock (key1); key2 is hashtext(pool_id).
_PD_LOCK_NAMESPACE = 778_010


def pool_delegation_length(pool: IpPool) -> int:
    """Per-customer PD size for a pool; defaults to /64."""
    value = getattr(pool, "delegation_prefix_length", None)
    try:
        length = int(value) if value else DEFAULT_DELEGATION_PREFIX_LENGTH
    except (TypeError, ValueError):
        length = DEFAULT_DELEGATION_PREFIX_LENGTH
    return length


def _parent_network(pool: IpPool) -> ipaddress.IPv6Network | None:
    try:
        net = ipaddress.ip_network(str(pool.cidr), strict=False)
    except ValueError:
        return None
    return net if isinstance(net, ipaddress.IPv6Network) else None


def iter_candidate_prefixes(pool: IpPool, *, cap: int = _MATERIALIZE_SCAN_CAP):
    """Yield up to ``cap`` aligned child networks of the pool's parent prefix at
    the configured delegation size (never overlapping)."""
    parent = _parent_network(pool)
    if parent is None:
        return
    length = pool_delegation_length(pool)
    if length < parent.prefixlen or length > 128:
        return
    yield from itertools.islice(parent.subnets(new_prefix=length), cap)


def _pool_advisory_lock(db: Session, pool_id) -> None:
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            select(
                func.pg_advisory_xact_lock(
                    _PD_LOCK_NAMESPACE, func.hashtext(str(pool_id))
                )
            )
        )


def active_delegated_prefix_for_subscriber(db: Session, subscriber_id) -> str | None:
    """The subscriber's assigned PD as a CIDR string (e.g. ``2001:db8:0:1::/64``),
    or None. This is what RADIUS emits as ``Delegated-IPv6-Prefix``."""
    if not subscriber_id:
        return None
    row = db.execute(
        select(Ipv6DelegatedPrefix.prefix, Ipv6DelegatedPrefix.prefix_length)
        .where(Ipv6DelegatedPrefix.subscriber_id == subscriber_id)
        .where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned)
        .order_by(Ipv6DelegatedPrefix.prefix)
        .limit(1)
    ).first()
    if not row:
        return None
    return f"{row[0]}/{row[1]}"


def allocate_delegated_prefix(
    db: Session,
    *,
    pool: IpPool,
    subscriber_id,
    subscription_id=None,
) -> Ipv6DelegatedPrefix | None:
    """Delegate a prefix to ``subscriber_id`` from ``pool``.

    Idempotent: returns the subscriber's existing assigned PD if any. Otherwise
    reuses a free (available) row, else materialises the next aligned prefix.
    Returns None when the pool is not a usable IPv6 parent or is exhausted.
    """
    if _parent_network(pool) is None:
        return None
    length = pool_delegation_length(pool)

    # Idempotent: the subscriber's existing assigned PD wins (IP stability).
    existing = db.execute(
        select(Ipv6DelegatedPrefix)
        .where(Ipv6DelegatedPrefix.pool_id == pool.id)
        .where(Ipv6DelegatedPrefix.subscriber_id == subscriber_id)
        .where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned)
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Serialise materialisation/pick for this pool across workers.
    _pool_advisory_lock(db, pool.id)

    # Reuse a released (available) row first.
    free = db.execute(
        select(Ipv6DelegatedPrefix)
        .where(Ipv6DelegatedPrefix.pool_id == pool.id)
        .where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.available)
        .order_by(Ipv6DelegatedPrefix.prefix)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if free is not None:
        free.state = Ipv6PrefixState.assigned
        free.subscriber_id = subscriber_id
        free.subscription_id = subscription_id
        db.flush()
        return free

    # Materialise the lowest aligned prefix not already in the table.
    taken = {
        prefix
        for (prefix,) in db.execute(
            select(Ipv6DelegatedPrefix.prefix).where(
                Ipv6DelegatedPrefix.pool_id == pool.id
            )
        ).all()
    }
    for candidate in iter_candidate_prefixes(pool):
        net_str = str(candidate.network_address)
        if net_str in taken:
            continue
        row = Ipv6DelegatedPrefix(
            pool_id=pool.id,
            prefix=net_str,
            prefix_length=length,
            state=Ipv6PrefixState.assigned,
            subscriber_id=subscriber_id,
            subscription_id=subscription_id,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            # Concurrent create of this prefix — try the next one.
            continue
        return row
    return None


def release_delegated_prefix(db: Session, prefix_row: Ipv6DelegatedPrefix) -> None:
    """Return a delegated prefix to the pool (available), unassigning its owner.
    The row is kept for reuse rather than deleted."""
    prefix_row.state = Ipv6PrefixState.available
    prefix_row.subscriber_id = None
    prefix_row.subscription_id = None
    db.flush()


def release_subscriber_prefixes(db: Session, subscriber_id) -> int:
    """Release all of a subscriber's assigned PDs (e.g. on terminal release)."""
    if not subscriber_id:
        return 0
    rows = (
        db.execute(
            select(Ipv6DelegatedPrefix)
            .where(Ipv6DelegatedPrefix.subscriber_id == subscriber_id)
            .where(Ipv6DelegatedPrefix.state == Ipv6PrefixState.assigned)
        )
        .scalars()
        .all()
    )
    for row in rows:
        release_delegated_prefix(db, row)
    return len(rows)
