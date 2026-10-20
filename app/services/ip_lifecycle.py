"""Service-IP lifecycle — release on terminal, plan the backlog.

Key invariant: **active service owns service IPs; terminal service does not.**

``release_service_ips_for_subscription`` is the forward fix: when a subscription
enters a terminal state (canceled / expired), the subscriber's
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
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.services.common import coerce_uuid
from app.services.network.ip import clear_released_ipv4_allocation_type

logger = logging.getLogger(__name__)

SAMPLE_LIMIT = 20

_TERMINAL = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    }
)
# Statuses that still entitle a subscriber to hold service IPs.
_NON_TERMINAL = frozenset(
    {
        SubscriptionStatus.active,
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.paused,
        SubscriptionStatus.stopped,
        SubscriptionStatus.pending,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
        SubscriptionStatus.disabled,
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
        select(IPAssignment, IPv4Address)
        .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
        .where(IPAssignment.subscriber_id == sid)
        .where(IPAssignment.is_active.is_(True))
        .where(IPAssignment.ip_version == IPVersion.ipv4)
    ).all()
    for assignment, address in v4_rows:
        if _is_reserved_v4(address.allocation_type, address.ont_unit_id):
            reserved_skipped += 1
            continue
        assignment.is_active = False
        clear_released_ipv4_allocation_type(db, address)
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


# --- Backlog cleanup apply (Track 3 part 2) ---------------------------------

# Only these planner classes are ever auto-applied. conflict_* and manual_review
# are NEVER touched by this tool.
APPLY_CLASSES = ("safe_release_terminal", "safe_dedupe_duplicate")


def apply_backlog_cleanup(
    db: Session,
    *,
    dry_run: bool = True,
    allowed_classes: tuple[str, ...] = APPLY_CLASSES,
) -> dict[str, Any]:
    """Deactivate the assignments the planner classifies as safe to release.

    DEFAULT IS DRY-RUN. Only ``allowed_classes`` (safe_release_terminal,
    safe_dedupe_duplicate) are ever applied; conflict_* and manual_review are
    never touched. Re-derives the plan at call time and re-validates each target
    is still active before deactivating. Returns before/after counts and the
    exact affected assignment ids (the rollback list)."""
    plan = plan_terminal_ip_backlog(db)
    counts_before = plan["counts"]
    targets = [it for it in plan["plan"] if it["classification"] in allowed_classes]
    target_ids = [it["assignment_id"] for it in targets]

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "allowed_classes": list(allowed_classes),
        "counts_before": counts_before,
        "target_count": len(targets),
        "target_assignment_ids": target_ids,
        "targets": targets,
        "released_assignment_ids": [],
        "released": 0,
    }
    if dry_run:
        return result

    released: list[str] = []
    for item in targets:
        assignment = db.get(IPAssignment, coerce_uuid(item["assignment_id"]))
        if assignment is not None and assignment.is_active:
            assignment.is_active = False
            if assignment.ip_version == IPVersion.ipv4:
                clear_released_ipv4_allocation_type(db, assignment.ipv4_address)
            released.append(str(assignment.id))
    if released:
        db.commit()
    result["released"] = len(released)
    result["released_assignment_ids"] = released
    result["counts_after"] = plan_terminal_ip_backlog(db)["counts"]
    return result


def reactivate_assignments(db: Session, assignment_ids: list[str]) -> int:
    """Rollback: restore ``is_active = True`` for the exact assignment ids from
    an apply manifest. Returns the number reactivated."""
    n = 0
    for aid in assignment_ids:
        assignment = db.get(IPAssignment, coerce_uuid(aid))
        if assignment is not None and not assignment.is_active:
            assignment.is_active = True
            n += 1
    if n:
        db.commit()
    return n


# --- Retention sweep (per-subscription IPv4 retention policy) ----------------
#
# READ side + dispatcher only. Every write goes through
# ``network.ip_assignment_lifecycle.repair_service_ipv4_assignment``; nothing in
# this section touches ``IPAssignment`` or ``Subscription`` columns.

RETENTION_STATUSES = (
    SubscriptionStatus.disabled,
    SubscriptionStatus.hidden,
    SubscriptionStatus.archived,
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
)
RETENTION_MODES = ("shadow", "apply")
RETENTION_CLASSES = (
    "would_release",
    "rebind_to_sibling",
    "skipped_live_conflict",
    "skipped_other",
)


def status_change_evidence(subscription: Subscription) -> datetime | None:
    """Best available evidence of when the subscription entered its status.

    canceled -> ``canceled_at``; expired -> ``end_at``; each falls back to
    ``updated_at``. disabled/hidden/archived have no status-change timestamp, so
    ``updated_at`` is used. Any later edit moves ``updated_at`` forward, so this
    evidence can only DELAY a release, never hasten one.
    """
    if subscription.status == SubscriptionStatus.canceled:
        return subscription.canceled_at or subscription.updated_at
    if subscription.status == SubscriptionStatus.expired:
        return subscription.end_at or subscription.updated_at
    return subscription.updated_at


def _retention_candidates(db: Session, *, cutoff: datetime, limit: int) -> list[Subscription]:
    """Bounded, oldest-evidence-first candidates with an exact active v4 row."""
    has_exact_v4 = (
        select(IPAssignment.id)
        .where(
            IPAssignment.subscription_id == Subscription.id,
            IPAssignment.is_active.is_(True),
            IPAssignment.ip_version == IPVersion.ipv4,
        )
        .exists()
    )
    old_enough = or_(
        and_(
            Subscription.status == SubscriptionStatus.canceled,
            func.coalesce(Subscription.canceled_at, Subscription.updated_at) <= cutoff,
        ),
        and_(
            Subscription.status == SubscriptionStatus.expired,
            func.coalesce(Subscription.end_at, Subscription.updated_at) <= cutoff,
        ),
        and_(
            Subscription.status.in_(
                (
                    SubscriptionStatus.disabled,
                    SubscriptionStatus.hidden,
                    SubscriptionStatus.archived,
                )
            ),
            Subscription.updated_at <= cutoff,
        ),
    )
    return list(
        db.scalars(
            select(Subscription)
            .where(Subscription.status.in_(RETENTION_STATUSES), old_enough, has_exact_v4)
            .order_by(Subscription.updated_at, Subscription.id)
            .limit(limit)
        )
    )


def run_ipv4_retention_release(
    db: Session,
    *,
    mode: str,
    retention_days: int,
    limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify (shadow) or release (apply) retention-expired IPv4 holds.

    ``shadow`` computes and reports only. ``apply`` releases ``would_release``
    subscriptions one at a time through the canonical owner command; sibling
    rebinds and live conflicts are never written (rebind is report-only in v1).
    Active, blocked, suspended, pending, paused and stopped subscriptions are
    never candidates.
    """
    from app.services import ip_assignment_lifecycle as owner
    from app.services.owner_commands import CommandContext

    if mode not in RETENTION_MODES:
        raise ValueError(f"unknown retention mode: {mode}")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    counts = dict.fromkeys(RETENTION_CLASSES, 0)
    counts.update(released=0, apply_failed=0)
    samples: dict[str, list[str]] = {c: [] for c in RETENTION_CLASSES}
    skipped_other: dict[str, int] = {}

    def _note(klass: str, subscription_id: Any) -> None:
        counts[klass] += 1
        if len(samples[klass]) < SAMPLE_LIMIT:
            samples[klass].append(str(subscription_id))

    # Capture plain values: the owner command refuses a caller-owned
    # transaction, so the session is rolled back before each apply and ORM
    # attributes must not be touched in between.
    candidates = [
        (row.id, row.subscriber_id, row.status)
        for row in _retention_candidates(db, cutoff=cutoff, limit=limit)
    ]
    db.rollback()
    for sub_id, subscriber_id, status in candidates:
        retention_status = status in (
            SubscriptionStatus.disabled,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        )
        exact = [
            item
            for item in owner._active_assignment_evidence(
                db, subscriber_id=subscriber_id, desired_address_id=None
            )
            if item.subscription_id == sub_id
        ]
        preview = owner.preview_service_ipv4_assignment_repair(
            db,
            subscription_id=sub_id,
            desired_address_id=None,
            deactivate_assignment_ids=tuple(item.assignment_id for item in exact),
            retention_cutoff=cutoff if retention_status else None,
        )
        decision = preview.decision
        live = None
        if decision is owner.IPv4AssignmentRepairDecision.ready_release:
            # Terminal (canceled/expired) release has no owner-side live-use
            # recheck, so the sweep checks before dispatching.
            live = owner.find_ipv4_live_use(
                db,
                subscription_id=sub_id,
                addresses={item.address for item in exact},
            )
            if live.in_use:
                decision = owner.IPv4AssignmentRepairDecision.address_in_live_use
        elif decision is owner.IPv4AssignmentRepairDecision.address_in_live_use:
            live = owner.find_ipv4_live_use(
                db,
                subscription_id=sub_id,
                addresses={item.address for item in exact},
            )

        if decision in (
            owner.IPv4AssignmentRepairDecision.ready_release,
            owner.IPv4AssignmentRepairDecision.ready_release_retention_expired,
        ):
            _note("would_release", sub_id)
            if mode == "apply":
                try:
                    db.rollback()
                    outcome = owner.repair_service_ipv4_assignment(
                        db,
                        owner.RepairServiceIPv4AssignmentCommand(
                            context=CommandContext.system(
                                actor="service:ipv4-retention-sweep",
                                scope="ip_assignment_lifecycle_repair",
                                reason=(
                                    f"IPv4 retention expired ({retention_days}d) "
                                    f"for {status.value} subscription"
                                ),
                                command_id=uuid.uuid4(),
                                idempotency_key=(
                                    f"ipv4-retention:{sub_id}:{preview.fingerprint[:32]}"
                                ),
                            ),
                            subscription_id=sub_id,
                            desired_address_id=None,
                            deactivate_assignment_ids=preview.deactivate_assignment_ids,
                            preview_fingerprint=preview.fingerprint,
                            retention_cutoff=cutoff if retention_status else None,
                        ),
                    )
                    counts["released"] += outcome.deactivated_count
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the sweep
                    db.rollback()
                    counts["apply_failed"] += 1
                    logger.warning(
                        "ipv4 retention release refused for %s: %s",
                        sub_id,
                        getattr(exc, "code", type(exc).__name__),
                    )
        elif decision is owner.IPv4AssignmentRepairDecision.address_in_live_use:
            sibling_live = False
            if live is not None and live.sibling_subscription_ids:
                sibling_live = bool(
                    db.scalar(
                        select(func.count())
                        .select_from(Subscription)
                        .where(
                            Subscription.id.in_(live.sibling_subscription_ids),
                            Subscription.subscriber_id == subscriber_id,
                            Subscription.status == SubscriptionStatus.active,
                        )
                    )
                )
            _note("rebind_to_sibling" if sibling_live else "skipped_live_conflict", sub_id)
        else:
            _note("skipped_other", sub_id)
            skipped_other[decision.value] = skipped_other.get(decision.value, 0) + 1

    return {
        "mode": mode,
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "examined": len(candidates),
        "limit": limit,
        "counts": counts,
        "skipped_other_by_decision": skipped_other,
        "samples": samples,
    }
