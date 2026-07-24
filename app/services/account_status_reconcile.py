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
from contextlib import nullcontext
from dataclasses import dataclass, field

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import AccessState, Subscription
from app.models.enforcement_lock import EnforcementLock
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.common import coerce_uuid
from app.services.notification_suppression import suppress_notifications
from app.services.radius_access_state import (
    ACTIVE_STATUSES,
    BLOCKED_STATUSES,
    TERMINATED_STATUSES,
    UNPROVISIONED_STATUSES,
)
from app.services.subscriber_access_policy import (
    RADIUS_BLOCKING_SUBSCRIBER_STATUSES,
)

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
    account_active_changed: bool = False
    access_states_changed: int = 0
    error: str | None = None


@dataclass
class DriftSummary:
    candidates: int = 0
    changed: int = 0
    account_active_changed: int = 0
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
    """Return whether canonical account or child access projections have drift."""
    from app.services.account_lifecycle import (
        derive_account_active_projection,
        derive_account_status,
        derive_subscription_access_projection,
    )

    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return False, "not_found"
    derived_status = derive_account_status(db, str(account.id))
    if account.status != derived_status:
        return True, None
    if account.is_active != derive_account_active_projection(
        account,
        account_status=derived_status,
    ):
        return True, None
    projected = derive_subscription_access_projection(
        db,
        account,
        account_status=derived_status,
    )
    actual = {
        subscription.id: subscription.access_state
        for subscription in db.scalars(
            select(Subscription).where(Subscription.subscriber_id == account.id)
        ).all()
    }
    if any(
        actual.get(subscription_id) != state
        for subscription_id, state in projected.items()
    ):
        return True, None
    return False, "already_converged"


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
    """Find broad SQL candidates; the lifecycle owner performs exact repair."""

    active_child = exists(
        select(Subscription.id).where(
            Subscription.subscriber_id == Subscriber.id,
            Subscription.status.in_(ACTIVE_STATUSES),
        )
    )
    active_lock = exists(
        select(EnforcementLock.id).where(
            EnforcementLock.subscription_id == Subscription.id,
            EnforcementLock.is_active.is_(True),
        )
    )
    child_projection_drift = exists(
        select(Subscription.id).where(
            Subscription.subscriber_id == Subscriber.id,
            or_(
                (
                    Subscription.status.in_(ACTIVE_STATUSES)
                    & Subscriber.status.in_(_PERMISSIVE_ACTIVE_PARENT_STATUSES)
                    & ~active_lock
                    & or_(
                        Subscription.access_state.is_(None),
                        Subscription.access_state != AccessState.active.value,
                    )
                ),
                (
                    Subscription.status.in_(ACTIVE_STATUSES)
                    & active_lock
                    & or_(
                        Subscription.access_state.is_(None),
                        Subscription.access_state.notin_(
                            {
                                AccessState.suspended.value,
                                AccessState.captive.value,
                            }
                        ),
                    )
                ),
                (
                    Subscription.status.in_(ACTIVE_STATUSES)
                    & Subscriber.status.in_(RADIUS_BLOCKING_SUBSCRIBER_STATUSES)
                    & or_(
                        Subscription.access_state.is_(None),
                        Subscription.access_state.notin_(
                            {
                                AccessState.suspended.value,
                                AccessState.captive.value,
                            }
                        ),
                    )
                ),
                (
                    Subscription.status.in_(BLOCKED_STATUSES)
                    & or_(
                        Subscription.access_state.is_(None),
                        Subscription.access_state.notin_(
                            {
                                AccessState.suspended.value,
                                AccessState.captive.value,
                            }
                        ),
                    )
                ),
                (
                    Subscription.status.in_(TERMINATED_STATUSES)
                    & or_(
                        Subscription.access_state.is_(None),
                        Subscription.access_state != AccessState.terminated.value,
                    )
                ),
                (
                    Subscription.status.in_(UNPROVISIONED_STATUSES)
                    & Subscription.access_state.isnot(None)
                ),
            ),
        )
    )
    parent_projection_drift = or_(
        (
            active_child
            & Subscriber.status.notin_(_PERMISSIVE_ACTIVE_PARENT_STATUSES)
            & Subscriber.lifecycle_override_status.is_(None)
        ),
        (~active_child & Subscriber.status.in_(_PERMISSIVE_ACTIVE_PARENT_STATUSES)),
    )
    account_active_drift = or_(
        (
            Subscriber.status.in_(
                {
                    SubscriberStatus.active,
                    SubscriberStatus.new,
                    SubscriberStatus.blocked,
                    SubscriberStatus.delinquent,
                }
            )
            & Subscriber.is_active.is_(False)
        ),
        (
            Subscriber.status.in_(
                {SubscriberStatus.disabled, SubscriberStatus.canceled}
            )
            & Subscriber.is_active.is_(True)
        ),
        (
            (Subscriber.status == SubscriberStatus.suspended)
            & (Subscriber.lifecycle_override_status == SubscriberStatus.suspended)
            & Subscriber.is_active.is_(True)
        ),
        (
            (Subscriber.status == SubscriberStatus.suspended)
            & or_(
                Subscriber.lifecycle_override_status.is_(None),
                Subscriber.lifecycle_override_status != SubscriberStatus.suspended,
            )
            & Subscriber.is_active.is_(False)
        ),
    )
    stmt = (
        select(Subscriber.id)
        .where(
            or_(
                parent_projection_drift,
                account_active_drift,
                child_projection_drift,
            )
        )
        .order_by(Subscriber.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [str(r[0]) for r in db.execute(stmt).all()]


def project_account(db: Session, account_id: str) -> DriftResult:
    """Read-only: report what the reconcile would derive, mutating nothing."""
    from app.services.account_lifecycle import (
        derive_account_active_projection,
        derive_account_status,
        derive_subscription_access_projection,
    )

    account = db.get(Subscriber, coerce_uuid(account_id))
    prior = account.status.value if account and account.status else "unknown"
    if account is None:
        return DriftResult(
            account_id=str(account_id),
            prior_status=prior,
            error="not_found",
        )
    projected = derive_account_status(db, str(account.id))
    access_projection = derive_subscription_access_projection(
        db,
        account,
        account_status=projected,
    )
    actual = {
        subscription.id: subscription.access_state
        for subscription in db.scalars(
            select(Subscription).where(Subscription.subscriber_id == account.id)
        ).all()
    }
    return DriftResult(
        account_id=str(account_id),
        prior_status=prior,
        new_status=projected.value,
        changed=projected != account.status,
        account_active_changed=(
            account.is_active
            != derive_account_active_projection(account, account_status=projected)
        ),
        access_states_changed=sum(
            actual.get(subscription_id) != access_state
            for subscription_id, access_state in access_projection.items()
        ),
    )


def reconcile_account(db: Session, account_id: str) -> DriftResult:
    """Re-derive one subscriber's status from its subscriptions (commits)."""
    from app.services.account_lifecycle import compute_account_status

    account = db.get(Subscriber, coerce_uuid(account_id))
    if account is None:
        return DriftResult(
            account_id=str(account_id),
            prior_status="unknown",
            error="not_found",
        )
    prior = account.status
    prior_active = account.is_active
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
        result.changed = new_status != prior
        result.account_active_changed = account.is_active != prior_active
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
    """Rebuild RADIUS through the canonical projection owner."""
    from app.services.radius_population import populate, require_complete_projection

    result = populate(dry_run=False)
    require_complete_projection(result)


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

    ``account_ids`` given → reconcile only accounts whose canonical parent,
    account-active, or child access projection differs from stored state.
    Already-converged accounts are recorded in ``summary.skipped`` and never
    mutated. ``limit`` is ignored when ``account_ids`` is given.

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
            if (
                result.changed
                or result.account_active_changed
                or result.access_states_changed
            ):
                if result.changed:
                    summary.changed += 1
                if result.account_active_changed:
                    summary.account_active_changed += 1
                coa_subscription_ids.update(_account_subscription_ids(db, account_id))
            summary.access_states_changed += result.access_states_changed

    if refresh_radius and (
        summary.changed
        or summary.access_states_changed
        or account_ids is not None
        or summary.account_active_changed
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
