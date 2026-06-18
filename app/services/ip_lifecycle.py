"""Service-IP lifecycle — release on terminal, plan the backlog.

Key invariant: **active service owns service IPs; terminal service does not.**

``release_service_ips_for_subscription`` is the forward fix: when a subscription
enters a terminal state (canceled / expired / disabled), the subscriber's
*service* IP assignments are deactivated — idempotently, and never touching a
management/ONT address or an IP still owned by another active subscription of
the same subscriber.

``plan_terminal_ip_backlog`` is the READ-ONLY planner for the existing backlog
(``terminal_subscription_active_ip_assignment`` + ``duplicate_active_ipv4_assignment``
from billing_integrity_audit). It classifies, it does not apply. See
docs/POST_CUTOVER_HARDENING.md.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion

logger = logging.getLogger(__name__)

SAMPLE_LIMIT = 20

_TERMINAL = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.disabled,
    }
)
# Statuses that still entitle a subscriber to hold service IPs.
_NON_TERMINAL = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.stopped,
        SubscriptionStatus.pending,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
    }
)


def _is_reserved_v4(allocation_type: str | None, ont_unit_id: Any) -> bool:
    """A v4 address that must never be released as a 'service' IP."""
    return ont_unit_id is not None or (allocation_type or "") == "management"


def _subscriber_has_non_terminal_sub(db: Session, subscriber_id: Any) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.subscriber_id == subscriber_id)
            .where(Subscription.status.in_(_NON_TERMINAL))
        )
    )


def release_service_ips_for_subscription(
    db: Session, subscription: Subscription
) -> dict[str, Any]:
    """Deactivate the subscriber's active *service* IP assignments when this
    subscription is terminal AND the subscriber has no remaining non-terminal
    subscription. Idempotent; flushes but does not commit.

    Skips management/ONT v4 addresses. Does not touch IPs while another active
    subscription of the subscriber could own them (subscriber-level guard —
    the safe grain while ``ip_assignments`` has no per-subscription column).
    """
    if subscription.status not in _TERMINAL:
        return {"released": 0, "skipped": "not_terminal"}
    sid = subscription.subscriber_id
    if sid is None:
        return {"released": 0, "skipped": "no_subscriber"}
    if _subscriber_has_non_terminal_sub(db, sid):
        return {"released": 0, "skipped": "subscriber_has_active_service"}

    released = 0
    reserved_skipped = 0

    # v4 — exclude management/ONT
    v4_rows = db.execute(
        select(IPAssignment, IPv4Address.allocation_type, IPv4Address.ont_unit_id)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.subscriber_id == sid)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all()
    for assignment, alloc, ont in v4_rows:
        if _is_reserved_v4(alloc, ont):
            reserved_skipped += 1
            continue
        assignment.is_active = False
        released += 1

    # v6 — no management/ONT marker exists on v6 addresses
    v6 = db.scalars(
        select(IPAssignment)
        .where(IPAssignment.subscriber_id == sid)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv6)
    ).all()
    for assignment in v6:
        assignment.is_active = False
        released += 1

    # Terminal service owns no IP — clear the cache columns too (idempotent).
    if subscription.ipv4_address:
        subscription.ipv4_address = None
    if subscription.ipv6_address:
        subscription.ipv6_address = None

    if released:
        db.flush()
        logger.info(
            "released %d service IP assignments for terminal subscriber %s "
            "(sub %s, %d reserved skipped)",
            released,
            sid,
            subscription.id,
            reserved_skipped,
        )
    return {"released": released, "reserved_skipped": reserved_skipped}


# --- Read-only backlog planner ----------------------------------------------

_PLAN_CLASSES = (
    "safe_release_terminal",
    "safe_dedupe_duplicate",
    "conflict_active_service",
    "conflict_management_or_ont",
    "manual_review",
)


def _norm(ip: str | None) -> str:
    return str(ip).strip() if ip else ""


def plan_terminal_ip_backlog(db: Session) -> dict[str, Any]:
    """READ-ONLY. Classify every subscriber holding active ipv4 assignments that
    are either terminal-held or duplicated. Returns
    ``{"counts": {class: n}, "samples": {class: [...]}, "plan": [items]}`` —
    one plan item per (subscriber, address). Applies nothing."""
    # All active ipv4 assignments with their address metadata.
    rows = db.execute(
        select(
            IPAssignment.subscriber_id,
            IPAssignment.id,
            IPv4Address.address,
            IPv4Address.allocation_type,
            IPv4Address.ont_unit_id,
        )
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all()
    by_subscriber: dict[str, list] = defaultdict(list)
    for sid, aid, address, alloc, ont in rows:
        if sid is not None:
            by_subscriber[str(sid)].append((str(aid), _norm(address), alloc, ont))

    non_terminal = {
        str(s)
        for (s,) in db.execute(
            select(Subscription.subscriber_id)
            .where(Subscription.status.in_(_NON_TERMINAL))
            .distinct()
        ).all()
        if s is not None
    }
    # Served IPs of ACTIVE subscriptions, mapped to their subscriber — to detect
    # an address a terminal holder is squatting that an active customer uses.
    served_active: dict[str, str] = {}
    for sid, ip in db.execute(
        select(Subscription.subscriber_id, Subscription.ipv4_address).where(
            Subscription.status == SubscriptionStatus.active
        )
    ).all():
        if sid is not None and _norm(ip):
            served_active.setdefault(_norm(ip), str(sid))

    plan: list[dict[str, Any]] = []
    for sub_id, assignments in by_subscriber.items():
        has_active = sub_id in non_terminal
        is_duplicate = len(assignments) > 1
        if has_active and not is_duplicate:
            continue  # single active assignment on a serviceable subscriber — fine
        # served IPs this subscriber's active subs use (to know which dup to keep)
        owned_served = {ip for ip, owner in served_active.items() if owner == sub_id}
        for aid, address, alloc, ont in assignments:
            cls = _classify_assignment(
                address,
                alloc,
                ont,
                has_active,
                is_duplicate,
                owned_served,
                served_active,
                sub_id,
            )
            plan.append(
                {
                    "subscriber_id": sub_id,
                    "assignment_id": aid,
                    "address": address,
                    "classification": cls,
                }
            )

    counts = dict.fromkeys(_PLAN_CLASSES, 0)
    samples: dict[str, list] = {c: [] for c in _PLAN_CLASSES}
    for item in plan:
        c = item["classification"]
        counts[c] += 1
        if len(samples[c]) < SAMPLE_LIMIT:
            samples[c].append(item["subscriber_id"])
    return {"counts": counts, "samples": samples, "plan": plan}


def _classify_assignment(
    address, alloc, ont, has_active, is_duplicate, owned_served, served_active, sub_id
) -> str:
    if _is_reserved_v4(alloc, ont):
        return "conflict_management_or_ont"
    other_owner = served_active.get(address)
    if other_owner is not None and other_owner != sub_id:
        return "conflict_active_service"  # an active customer elsewhere uses this IP
    if not has_active:
        return "safe_release_terminal"
    # has an active subscription + duplicate assignments
    if is_duplicate:
        if owned_served and address not in owned_served:
            return "safe_dedupe_duplicate"  # not the served one → drop the extra
        if owned_served and address in owned_served:
            return "manual_review"  # the served one — keep it, not a release target
    return "manual_review"
