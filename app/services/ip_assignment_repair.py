"""Step 2a — repair the IPAM ledger to match what RADIUS actually serves.

The 2026-06-17 prod audit (``ip_consistency_audit``) found 593/4022 active subs
whose ``IPAssignment`` set disagrees with the served IPv4 (the
``subscription.ipv4_address`` column, which equals the external radreply
Framed-IP for every sub). The served value is what the customer routes on right
now, so it is the operational truth; the IPAM set is the drifted, neglected
side. See ``docs/designs/SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md`` §5b.

This module makes the *ledger* reflect that reality — it NEVER changes a served
IP, so it is non-customer-impacting:

  - ``backfill_create``  — active sub with a served IP but no active IPAM row →
    create an ``IPAssignment`` (and ``IPv4Address`` if absent) for the served IP.
  - ``repoint``          — active IPAM row exists but for a different address →
    deactivate it and ensure an assignment to the served IP instead.

It refuses to auto-fix genuine conflicts (served IP already actively assigned to
another subscriber, a management/ONT address, or a subscriber with multiple
active subs claiming different IPs) — those are reported for human review.

Dry-run by design: ``plan_repair`` only reads. ``apply_repair`` writes, and the
CLI requires an explicit flag. Idempotent: a repaired subscriber re-plans as
``noop_already_correct``.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.services.common import coerce_uuid
from app.services.ip_consistency_audit import _norm

logger = logging.getLogger(__name__)

# Writable outcomes vs reported-only conflicts.
#
#   backfill_create — subscriber has no IPAM row; the served address is free.
#   repoint         — subscriber's own IPAM row points at the wrong address.
#   reclaim_stale   — the served address is held by ANOTHER subscriber whose own
#                     served IP is different (or who has no active service), so
#                     their claim is provably stale (the asymmetric-release bug).
#                     Repoint that one address row to the real served owner.
#   dedupe_active   — the served address is already active for this subscriber,
#                     but they carry EXTRA active ipv4 assignments (stale rows
#                     never released). Deactivate the extras, keep the served one.
ACTIONABLE = ("backfill_create", "repoint", "reclaim_stale", "dedupe_active")
CONFLICTS = (
    "conflict_live_contention",  # owner is ALSO served this address — real clash
    "conflict_addr_reserved",  # management / ONT address
    "conflict_ambiguous_multi_active",  # subscriber claims two different IPs
)
NOOP = ("noop_already_correct",)


def _ipv4_pools(db: Session) -> list[tuple[IpPool, Any]]:
    """Active ipv4 pools paired with their parsed network, for best-effort
    pool/gateway/prefix backfill on newly-created addresses."""
    pools: list[tuple[IpPool, Any]] = []
    for pool in db.scalars(
        select(IpPool)
        .where(IpPool.is_active.is_(True))
        .where(IpPool.ip_version == IPVersion.ipv4)
    ):
        try:
            pools.append((pool, ipaddress.ip_network(pool.cidr, strict=False)))
        except ValueError:
            continue
    return pools


def _match_pool(
    ip: str, pools: list[tuple[IpPool, Any]]
) -> tuple[IpPool | None, int | None]:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None, None
    for pool, net in pools:
        if addr in net:
            return pool, net.prefixlen
    return None, None


def _served_ip_by_subscriber(db: Session) -> tuple[dict[str, str], set[str]]:
    """Map subscriber_id → served ipv4 (the active sub's column) and the set of
    subscribers with conflicting served IPs across multiple active subs."""
    by_subscriber: dict[str, str] = {}
    ambiguous: set[str] = set()
    rows = db.execute(
        select(Subscription.subscriber_id, Subscription.ipv4_address).where(
            Subscription.status == SubscriptionStatus.active
        )
    ).all()
    for subscriber_id, col_ip_raw in rows:
        if subscriber_id is None:
            continue
        served = _norm(col_ip_raw)
        if not served:
            continue
        sid = str(subscriber_id)
        existing = by_subscriber.get(sid)
        if existing and existing != served:
            ambiguous.add(sid)
        else:
            by_subscriber[sid] = served
    return by_subscriber, ambiguous


def _active_ipv4_assignments(
    db: Session,
) -> dict[str, list[tuple[str, str]]]:
    """subscriber_id → list of (assignment_id, address) for active ipv4."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    rows = db.execute(
        select(IPAssignment.subscriber_id, IPAssignment.id, IPv4Address.address)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all()
    for subscriber_id, assignment_id, address in rows:
        if subscriber_id is not None:
            out[str(subscriber_id)].append((str(assignment_id), _norm(address)))
    return out


def _classify_target_address(
    db: Session, subscriber_id: str, desired_ip: str, served: dict[str, str]
) -> str | None:
    """Inspect the IPv4Address row for ``desired_ip``. Returns a *conflict*
    string if it can't be safely claimed, ``"reclaim_stale"`` if it's held by a
    provably-stale other owner, or None if it's free / already ours."""
    addr = db.scalars(
        select(IPv4Address).where(IPv4Address.address == desired_ip)
    ).first()
    if addr is None:
        return None  # free — will be created fresh
    if addr.ont_unit_id is not None or (addr.allocation_type or "") == "management":
        return "conflict_addr_reserved"
    existing = addr.assignment
    if existing is None or str(existing.subscriber_id) == subscriber_id:
        return None  # ours (active or inactive) — reuse
    # Held by another subscriber. Stale unless that owner is ALSO served this IP.
    owner_served = _norm(served.get(str(existing.subscriber_id)) or "")
    if owner_served == desired_ip:
        return "conflict_live_contention"
    return "reclaim_stale"


def plan_repair(db: Session) -> dict[str, Any]:
    """Read-only. Classify every active pinned-IP subscriber and return a plan
    (per-item actions + summary counts). Mirrors the audit population."""
    served, ambiguous = _served_ip_by_subscriber(db)
    active_assign = _active_ipv4_assignments(db)

    items: list[dict[str, Any]] = []
    for sid, desired_ip in served.items():
        current = active_assign.get(sid, [])
        current_ips = [ip for _, ip in current]
        item: dict[str, Any] = {
            "subscriber_id": sid,
            "desired_ip": desired_ip,
            "current_ipam_ips": current_ips,
        }
        if sid in ambiguous:
            item["action"] = "conflict_ambiguous_multi_active"
            items.append(item)
            continue
        if desired_ip in current_ips:
            # Correct address is active — but extra active ipv4 rows are stale
            # cruft (asymmetric-release leftovers) that confuse readers.
            item["action"] = (
                "dedupe_active" if len(current) > 1 else "noop_already_correct"
            )
            items.append(item)
            continue
        classified = _classify_target_address(db, sid, desired_ip, served)
        if classified in CONFLICTS:
            item["action"] = classified
            items.append(item)
            continue
        if classified == "reclaim_stale":
            item["action"] = "reclaim_stale"
        else:
            item["action"] = "backfill_create" if not current else "repoint"
        items.append(item)

    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it["action"]] += 1

    return {
        "population": len(items),
        "counts": dict(counts),
        "actionable": sum(counts.get(a, 0) for a in ACTIONABLE),
        "conflicts": sum(counts.get(c, 0) for c in CONFLICTS),
        "items": items,
    }


def _ensure_assignment(
    db: Session,
    subscriber_id: str,
    desired_ip: str,
    pools: list[tuple[IpPool, Any]],
    served: dict[str, str],
) -> bool:
    """Ensure the subscriber has an active ipv4 IPAssignment for ``desired_ip``,
    creating the address/assignment or reclaiming a provably-stale one as
    needed, and deactivate the subscriber's other active ipv4 assignments.
    Returns True if anything changed; False (no write) if the target can't be
    safely claimed (reserved, or live contention)."""
    sub_uuid = coerce_uuid(subscriber_id)
    target_subscription = db.scalars(
        select(Subscription)
        .where(Subscription.subscriber_id == sub_uuid)
        .where(Subscription.status == SubscriptionStatus.active)
        .where(Subscription.ipv4_address == desired_ip)
    ).first()
    addr = db.scalars(
        select(IPv4Address).where(IPv4Address.address == desired_ip)
    ).first()

    if addr is None:
        pool, prefix = _match_pool(desired_ip, pools)
        addr = IPv4Address(
            address=desired_ip,
            pool_id=pool.id if pool else None,
            allocation_type="static",
        )
        db.add(addr)
        db.flush()
        assignment = None
    else:
        if addr.ont_unit_id is not None or (addr.allocation_type or "") == "management":
            return False
        assignment = addr.assignment
        if assignment is not None and str(assignment.subscriber_id) != subscriber_id:
            # Held by another subscriber. Only reclaim if their claim is stale
            # (they are not actually served this IP) — re-checked here so a
            # plan/apply race can never steal a live IP.
            owner_served = _norm(served.get(str(assignment.subscriber_id)) or "")
            if owner_served == desired_ip:
                return False  # live contention — refuse
            assignment.subscriber_id = sub_uuid  # reclaim the single address row
            assignment.subscription_id = (
                target_subscription.id if target_subscription else None
            )
        pool = db.get(IpPool, addr.pool_id) if addr.pool_id else None
        prefix = None
        if pool:
            _, prefix = _match_pool(
                desired_ip, [(pool, ipaddress.ip_network(pool.cidr, strict=False))]
            )

    if assignment is None:
        assignment = IPAssignment(
            subscriber_id=sub_uuid,
            subscription_id=target_subscription.id if target_subscription else None,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=addr.id,
            is_active=True,
            prefix_length=prefix,
            gateway=pool.gateway if pool else None,
            dns_primary=pool.dns_primary if pool else None,
            dns_secondary=pool.dns_secondary if pool else None,
        )
        db.add(assignment)
    else:
        assignment.subscriber_id = sub_uuid
        assignment.subscription_id = (
            target_subscription.id if target_subscription else None
        )
        assignment.is_active = True

    # Deactivate the subscriber's OTHER active ipv4 assignments (the stale IP).
    others = db.scalars(
        select(IPAssignment)
        .where(IPAssignment.subscriber_id == sub_uuid)
        .where(IPAssignment.ip_version == IPVersion.ipv4)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ipv4_address_id != addr.id)
    ).all()
    for other in others:
        other.is_active = False

    return True


def apply_repair(
    db: Session, plan: dict[str, Any], *, limit: int | None = None
) -> dict[str, Any]:
    """Execute the actionable items of ``plan``. Writes the IPAM ledger only —
    never a served IP. Commits per item so a mid-run failure leaves prior
    repairs durable. ``limit`` caps the number of subscribers repaired."""
    applied = {
        "backfill_create": 0,
        "repoint": 0,
        "reclaim_stale": 0,
        "dedupe_active": 0,
        "skipped": 0,
        "errors": 0,
    }
    done = 0
    pools = _ipv4_pools(db)
    served, _ = _served_ip_by_subscriber(db)
    for item in plan["items"]:
        if item["action"] not in ACTIONABLE:
            continue
        if limit is not None and done >= limit:
            break
        try:
            changed = _ensure_assignment(
                db, item["subscriber_id"], item["desired_ip"], pools, served
            )
            if changed:
                db.commit()
                applied[item["action"]] += 1
                done += 1
            else:
                db.rollback()
                applied["skipped"] += 1
        except Exception:
            db.rollback()
            applied["errors"] += 1
            logger.exception(
                "IPAM repair failed for subscriber %s (desired %s)",
                item["subscriber_id"],
                item["desired_ip"],
            )
    applied["subscribers_repaired"] = done
    return applied
