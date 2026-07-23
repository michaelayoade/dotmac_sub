"""Reconcile subscriber/access projections from canonical lifecycle facts.

``Subscriber.status`` and ``Subscription.access_state`` are projections owned by
``access.subscription_lifecycle``.  A parent without an explicit lifecycle
override cannot remain in a RADIUS-blocking status while any child service is
active: ``derive_account_status`` deliberately gives an active service priority
and preserves ``delinquent`` only as the permissive dunning state.

This module is the bounded recovery adapter for a missed transition event.  It
invokes the same lifecycle owner used in-line by payment, renewal, dunning and
admin commands; it does not introduce a second access policy or write money.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from importlib import import_module
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import coerce_uuid
from app.services.notification_suppression import suppress_notifications

logger = logging.getLogger(__name__)

_PERMISSIVE_ACTIVE_PARENT_STATUSES = frozenset(
    {SubscriberStatus.active, SubscriberStatus.delinquent}
)


@dataclass
class DriftResult:
    account_id: str
    prior_status: str
    new_status: str | None = None
    changed: bool = False
    access_states_changed: int = 0
    error: str | None = None


@dataclass
class DriftSummary:
    candidates: int = 0
    changed: int = 0
    access_states_changed: int = 0
    errors: int = 0
    dry_run: bool = True
    radius_refreshed: bool = False
    sessions_kicked: int = 0
    results: list[DriftResult] = field(default_factory=list)
    # Explicitly-requested accounts that failed the eligibility filter, with the
    # reason. Only populated when ``account_ids`` is passed.
    skipped: list[dict] = field(default_factory=list)


def account_eligibility(db: Session, account_id: str) -> tuple[bool, str | None]:
    """Return whether an account has safe, derived parent-status drift.

    Explicit overrides remain authoritative.  Otherwise any active child makes
    a blocking parent a stale projection, even when another child is suspended;
    per-service restrictions remain on that child and are not cleared here.
    """
    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return False, "not_found"
    if account.lifecycle_override_status is not None:
        return False, "explicit_lifecycle_override"
    if account.status in _PERMISSIVE_ACTIVE_PARENT_STATUSES:
        status = account.status.value if account.status else "unknown"
        return False, f"parent_already_permissive (status={status})"
    statuses = [
        r[0]
        for r in db.execute(
            select(Subscription.status).where(Subscription.subscriber_id == account.id)
        ).all()
    ]
    if not any(status == SubscriptionStatus.active for status in statuses):
        return False, "no_active_subscription"
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


def find_account_projection_drift_ids(
    db: Session, *, limit: int | None = None
) -> list[str]:
    """Blocking parents with an active child and no explicit override."""
    stmt = (
        select(Subscriber.id)
        .join(Subscription, Subscription.subscriber_id == Subscriber.id)
        .where(
            Subscription.status == SubscriptionStatus.active,
            Subscriber.status.notin_(_PERMISSIVE_ACTIVE_PARENT_STATUSES),
        )
        .where(Subscriber.lifecycle_override_status.is_(None))
        .distinct()
        .order_by(Subscriber.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [str(r[0]) for r in db.execute(stmt).all()]


def project_account(db: Session, account_id: str) -> DriftResult:
    """Read-only: report what the reconcile would derive, mutating nothing."""
    from app.services.account_lifecycle import derive_account_status

    account = db.get(Subscriber, coerce_uuid(account_id))
    prior = account.status.value if account and account.status else "unknown"
    if account is None:
        return DriftResult(
            account_id=str(account_id),
            prior_status=prior,
            error="not_found",
        )
    projected = derive_account_status(db, str(account.id))
    return DriftResult(
        account_id=str(account_id),
        prior_status=prior,
        new_status=projected.value,
        changed=projected != account.status,
    )


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
        before_access = {
            str(subscription.id): subscription.access_state
            for subscription in db.scalars(
                select(Subscription).where(
                    Subscription.subscriber_id == coerce_uuid(account_id)
                )
            ).all()
        }
        new_status = compute_account_status(db, str(account_id))
        result.new_status = new_status.value
        result.changed = prior is not None and new_status != prior
        after_access = {
            str(subscription.id): subscription.access_state
            for subscription in db.scalars(
                select(Subscription).where(
                    Subscription.subscriber_id == coerce_uuid(account_id)
                )
            ).all()
        }
        result.access_states_changed = sum(
            before_access.get(subscription_id) != access_state
            for subscription_id, access_state in after_access.items()
        )
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
    """Reconcile safe parent/access drift, then refresh RADIUS + CoA.

    ``account_ids`` given → reconcile ONLY those that pass the same eligibility
    filter (active child, blocking parent, no explicit override); ineligible
    accounts are recorded in ``summary.skipped`` and never mutated. ``limit`` is
    ignored when ``account_ids`` is given.

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
        targets = find_account_projection_drift_ids(db, limit=limit)
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
            if result.changed or result.access_states_changed:
                if result.changed:
                    summary.changed += 1
                coa_subscription_ids.update(_account_subscription_ids(db, account_id))
            summary.access_states_changed += result.access_states_changed

    if refresh_radius and (
        summary.changed or summary.access_states_changed or account_ids is not None
    ):
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
