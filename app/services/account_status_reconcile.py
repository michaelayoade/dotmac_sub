"""Reconcile stale subscriber-level block drift against subscription state.

A subscriber whose ``status='blocked'`` while ALL of its subscriptions are
``active`` is denormalization drift: ``compute_account_status`` derives the
account from its subscriptions, so for this cohort the subscriptions are the
authority and the account flag is simply stale. Until it is corrected the
subscriber is walled-gardened at the BNG regardless of the active subscription —
``radius_population._radreply_attrs`` keys the suspended Mikrotik-Address-List on
``Subscriber.status == blocked`` directly (radius_population.py:104).

This module re-derives the account status from its subscriptions and then
refreshes RADIUS + CoA, reusing the proven batch shape of
``billing/unwall_paid_accounts.py`` (dry-run/apply, notification suppression,
ONE full RADIUS rebuild, CoA afterwards) — but WITHOUT its paid-balance gate.
The cohort here is defined by subscription state, not money, so a balance gate
would wrongly hold back accounts that are unambiguously mis-flagged.

SCOPE: only ``blocked`` subscribers whose subscriptions are ALL active are
auto-reconciled. Mixed-status accounts (some active, some blocked/suspended) are
deliberately excluded — their block may be legitimate per-service and needs
review, not a blanket flip.

No ledger / money writes; pure service-state correction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from importlib import import_module
from typing import cast

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import coerce_uuid
from app.services.notification_suppression import suppress_notifications

logger = logging.getLogger(__name__)


@dataclass
class DriftResult:
    account_id: str
    prior_status: str
    new_status: str | None = None
    changed: bool = False
    error: str | None = None


@dataclass
class DriftSummary:
    candidates: int = 0
    changed: int = 0
    errors: int = 0
    dry_run: bool = True
    radius_refreshed: bool = False
    sessions_kicked: int = 0
    results: list[DriftResult] = field(default_factory=list)
    # Explicitly-requested accounts that failed the eligibility filter, with the
    # reason. Only populated when ``account_ids`` is passed.
    skipped: list[dict] = field(default_factory=list)


def account_eligibility(db: Session, account_id: str) -> tuple[bool, str | None]:
    """Per-account eligibility for the all-active reconcile.

    Eligible = a ``blocked`` subscriber whose subscriptions are ALL active (the
    same rule as ``find_blocked_all_active_account_ids``, but for one explicitly
    named account). Returns ``(eligible, reason_if_not)``. This is the guard that
    keeps a targeted ``--account-ids`` run from flipping a mixed/active account:
    ``reconcile_account`` alone would still derive ``active`` for any account with
    one active sub, so the eligibility filter — not the derivation — is the safety.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return False, "not_found"
    if account.status != SubscriberStatus.blocked:
        status = account.status.value if account.status else "unknown"
        return False, f"not_blocked (status={status})"
    statuses = [
        r[0]
        for r in db.execute(
            select(Subscription.status).where(Subscription.subscriber_id == account.id)
        ).all()
    ]
    active = sum(1 for s in statuses if s == SubscriptionStatus.active)
    if active == 0:
        return False, "no_active_subscription"
    if active != len(statuses):
        return False, "mixed_status (has non-active subscriptions)"
    return True, None


def _partition_requested(
    db: Session, account_ids: list[str]
) -> tuple[list[str], list[dict]]:
    """Split explicitly-requested account_ids into (eligible, skipped+reason)."""
    eligible: list[str] = []
    skipped: list[dict] = []
    for aid in account_ids:
        ok, reason = account_eligibility(db, aid)
        if ok:
            eligible.append(aid)
        else:
            skipped.append({"account_id": str(aid), "reason": reason})
    return eligible, skipped


def find_blocked_all_active_account_ids(
    db: Session, *, limit: int | None = None
) -> list[str]:
    """Subscribers ``status='blocked'`` whose subscriptions are ALL active.

    Requires at least one subscription and zero non-active subscriptions, so a
    mixed-status account (where the block may be legitimate) is never returned.
    Uses ``SUM(CASE ...)`` rather than aggregate ``FILTER`` for SQLite parity.
    """
    active_count = func.sum(
        case((Subscription.status == SubscriptionStatus.active, 1), else_=0)
    )
    total_count = func.count(Subscription.id)

    sub_rollup = (
        select(
            Subscription.subscriber_id.label("subscriber_id"),
            total_count.label("total"),
            active_count.label("active"),
        )
        .group_by(Subscription.subscriber_id)
        .subquery()
    )

    stmt = (
        select(Subscriber.id)
        .join(sub_rollup, sub_rollup.c.subscriber_id == Subscriber.id)
        .where(Subscriber.status == SubscriberStatus.blocked)
        .where(sub_rollup.c.active > 0)
        .where(sub_rollup.c.total == sub_rollup.c.active)
        .order_by(Subscriber.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [str(r[0]) for r in db.execute(stmt).all()]


def project_account(db: Session, account_id: str) -> DriftResult:
    """Read-only: report what the reconcile would derive, mutating nothing."""
    account = db.get(Subscriber, coerce_uuid(account_id))
    prior = account.status.value if account and account.status else "unknown"
    return DriftResult(account_id=str(account_id), prior_status=prior)


def reconcile_account(db: Session, account_id: str) -> DriftResult:
    """Re-derive one subscriber's status from its subscriptions (commits)."""
    from app.services.account_lifecycle import compute_account_status

    account = db.get(Subscriber, coerce_uuid(account_id))
    prior = account.status if account else None
    result = DriftResult(
        account_id=str(account_id),
        prior_status=prior.value if prior else "unknown",
    )
    try:
        new_status = compute_account_status(db, str(account_id))
        result.new_status = new_status.value
        result.changed = prior is not None and new_status != prior
        db.commit()
    except Exception as exc:  # noqa: BLE001 — isolate one bad account from the batch
        db.rollback()
        result.error = str(exc)
        logger.exception("Status reconcile failed for account %s", account_id)
    return result


def _account_subscription_ids(db: Session, account_id: str) -> list[str]:
    return [
        str(r[0])
        for r in db.execute(
            select(Subscription.id).where(
                Subscription.subscriber_id == coerce_uuid(account_id)
            )
        ).all()
    ]


def _default_refresh_radius() -> None:
    """Lazy default: rebuild RADIUS from authoritative state.

    Imported lazily and only on the apply path so the status-repair logic (and
    its tests) never depend on the RADIUS-sweep module at import time — apply
    mode legitimately needs it, dry-run does not.

    Prefers the relocated ``app.services.radius_population`` module; falls back to
    the committed ``scripts.migration.populate_radius_from_subs`` (same logic,
    its current home on ``main``). Both expose ``populate(dry_run=...)``, so this
    one file works before and after the legacy-decommission relocation lands.
    """
    try:
        module = import_module("app.services.radius_population")
    except ImportError:
        module = import_module("scripts.migration.populate_radius_from_subs")

    populate_radius = cast(Callable[..., object], module.populate)
    populate_radius(dry_run=False)


def _default_coa(db: Session, subscription_id: str, *, reason: str) -> int:
    from app.services.enforcement import disconnect_subscription_sessions

    return disconnect_subscription_sessions(db, subscription_id, reason=reason)


def reconcile_cohort(
    db: Session,
    *,
    account_ids: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    refresh_radius: bool = True,
    send_coa: bool = True,
    notify: bool = False,
    refresh_fn=None,
    coa_fn=None,
) -> DriftSummary:
    """Reconcile blocked-but-all-active subscribers, then refresh RADIUS + CoA.

    ``account_ids`` given → reconcile ONLY those that pass the same eligibility
    filter (blocked subscriber, all subs active); ineligible ones are recorded in
    ``summary.skipped`` with a reason and NEVER mutated (the filter — not the
    derivation — is the safety, since ``reconcile_account`` would otherwise flip
    any account with one active sub). ``limit`` is ignored when ``account_ids`` is
    given. Otherwise discover the full cohort via
    ``find_blocked_all_active_account_ids``.

    ``notify`` defaults False — this is a bulk catch-up, so suppress the
    "service resumed" burst. RADIUS is rebuilt ONCE after all status writes, then
    CoA kicks the affected subscriptions so the stale walled-garden tag drops.

    ``refresh_fn`` / ``coa_fn`` are injectable so callers/tests can decouple the
    apply path from the RADIUS-sweep module: ``refresh_fn()`` rebuilds RADIUS,
    ``coa_fn(db, subscription_id, reason=...)`` kicks a session and returns a
    count. Both default to the real implementations (lazily imported).
    """
    skipped: list[dict] = []
    if account_ids is not None:
        targets, skipped = _partition_requested(db, account_ids)
    else:
        targets = find_blocked_all_active_account_ids(db, limit=limit)
    summary = DriftSummary(candidates=len(targets), dry_run=dry_run)
    summary.skipped = skipped

    if dry_run:
        summary.results = [project_account(db, aid) for aid in targets]
        return summary

    suppress_ctx = nullcontext() if notify else suppress_notifications()
    coa_subscription_ids: set = set()
    with suppress_ctx:
        for account_id in targets:
            result = reconcile_account(db, account_id)
            summary.results.append(result)
            if result.error:
                summary.errors += 1
                continue
            if result.changed:
                summary.changed += 1
                coa_subscription_ids.update(_account_subscription_ids(db, account_id))

    if refresh_radius and (summary.changed or account_ids is not None):
        (refresh_fn or _default_refresh_radius)()
        summary.radius_refreshed = True

    if send_coa and coa_subscription_ids:
        coa = coa_fn or _default_coa
        kicked = 0
        for subscription_id in coa_subscription_ids:
            try:
                kicked += coa(
                    db, subscription_id, reason="subscriber-status drift reconcile"
                )
            except Exception:
                logger.warning(
                    "Drift reconcile: CoA kick failed for subscription %s",
                    subscription_id,
                    exc_info=True,
                )
        summary.sessions_kicked = kicked

    return summary
