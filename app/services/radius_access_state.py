"""Derive and apply the RADIUS access state.

``derive_access_state`` — pure mapping (phase 2).
``set_subscription_access_state`` — dual-write app DB ``access_state``
+ external RADIUS ``radusergroup`` (phase 3, shadow).

See ``docs/radius_state_refactor/phase0_state_model.md``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Column, Integer, String, delete, insert, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_lock import AccessRestrictionMode
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

logger = logging.getLogger(__name__)

AccessStateWriteResult = dict[str, int | str | None]

# Status sets — declared here as constants so callers can also reason
# about which SubscriptionStatus values map to a given AccessState
# without inverting the function.

_ACTIVE_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.active,
    }
)

_BLOCKED_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.suspended,
        SubscriptionStatus.blocked,
        SubscriptionStatus.stopped,
    }
)

_TERMINATED_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.disabled,
    }
)

# Pending/hidden/archived → None. Not provisioned to RADIUS.
_UNPROVISIONED_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.pending,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
    }
)

# ---------------------------------------------------------------------------
# Canonical status → desired-connectivity classification (single source of
# truth). Other modules MUST reference these instead of redefining their own
# status sets — those copies had drifted and disagreed (e.g. radius_reject
# omitted ``stopped``/``disabled``; the suspension audit omitted ``disabled``),
# which is how terminal subscribers kept connectivity past their transition.
# The four sets are mutually exclusive and exhaustive over SubscriptionStatus.
# ---------------------------------------------------------------------------
ACTIVE_STATUSES = _ACTIVE_STATUSES
BLOCKED_STATUSES = _BLOCKED_STATUSES
TERMINATED_STATUSES = _TERMINATED_STATUSES
UNPROVISIONED_STATUSES = _UNPROVISIONED_STATUSES

# Any subscriber whose only relevant statuses are here should have NO normal
# (unrestricted) RADIUS access — either walled-garden (blocked) or removed
# (terminated).
NO_ACCESS_STATUSES = _BLOCKED_STATUSES | _TERMINATED_STATUSES

# Exhaustiveness guard: every SubscriptionStatus must be classified exactly
# once, so a newly-added status can't silently fall through to "no rule".
_ALL_CLASSIFIED = (
    _ACTIVE_STATUSES
    | _BLOCKED_STATUSES
    | _TERMINATED_STATUSES
    | _UNPROVISIONED_STATUSES
)
_UNCLASSIFIED = set(SubscriptionStatus) - _ALL_CLASSIFIED
if _UNCLASSIFIED:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        f"Unclassified SubscriptionStatus in connectivity map: {_UNCLASSIFIED}"
    )


# Map AccessState → external RADIUS group name. Terminated and None
# both mean "no row in radusergroup", which causes auth to fail with
# user-not-found at next attempt.
_GROUP_FOR_STATE: dict[AccessState, str] = {
    AccessState.active: "dotmac-active",
    AccessState.suspended: "dotmac-suspended",
    AccessState.captive: "dotmac-captive",
    AccessState.terminated: "",  # sentinel — delete only, don't insert
}


def derive_access_state(
    subscription_status: SubscriptionStatus,
    *,
    restriction_mode: AccessRestrictionMode | None = None,
    hard_reject: bool = False,
) -> AccessState | None:
    """Pure mapping: subscription.status (+ flags) → AccessState.

    Returns None when the subscription is not provisioned to RADIUS yet
    (pending, hidden, archived). Callers should treat None as "no
    radusergroup row should exist for this user".

    Blocked statuses map to ``captive`` only after the canonical walled-garden
    policy resolved a persisted restriction to that effective mode.
    """
    if subscription_status in _ACTIVE_STATUSES:
        return AccessState.active
    if subscription_status in _BLOCKED_STATUSES:
        if restriction_mode is None:
            restriction_mode = AccessRestrictionMode.hard_reject
        if hard_reject:
            restriction_mode = AccessRestrictionMode.hard_reject
        return (
            AccessState.captive
            if restriction_mode == AccessRestrictionMode.captive
            else AccessState.suspended
        )
    if subscription_status in _TERMINATED_STATUSES:
        return AccessState.terminated
    # Unprovisioned (pending/hidden/archived) or any future
    # SubscriptionStatus value we don't know yet → None.
    return None


# Subscriber-level aggregation priority. AccessCredential belongs to
# a subscriber, not a subscription — so when a subscriber has multiple
# subscriptions in different states, their auth state must be the
# "best" (most permissive) of those derived per-sub states. A
# subscriber with any active sub is "active", with captive but no
# active is "captive", etc. Terminated wins only when every sub is
# terminated.
_STATE_PRIORITY: tuple[AccessState, ...] = (
    AccessState.active,
    AccessState.captive,
    AccessState.suspended,
    AccessState.terminated,
)


def derive_subscriber_access_state(
    db: Session, subscriber_id: Any
) -> AccessState | None:
    """Aggregate per-subscription derived states across all of a
    subscriber's subscriptions to produce the subscriber-level access
    state. Returns the most-permissive state across all subs.

    Returns None only when the subscriber has zero subs, OR when every
    sub maps to None (all pending/hidden/archived).
    """
    subscriptions = list(
        db.scalars(
            select(Subscription).where(
                Subscription.subscriber_id == coerce_uuid(subscriber_id)
            )
        ).all()
    )
    if not subscriptions:
        return None
    subscriber = db.get(Subscriber, coerce_uuid(subscriber_id))
    from app.services.walled_garden_policy import resolve_subscription_restriction

    states = set()
    for subscription in subscriptions:
        restriction = resolve_subscription_restriction(
            db,
            subscription,
            account=subscriber,
        )
        states.add(
            derive_access_state(
                subscription.status,
                restriction_mode=(restriction.effective_mode if restriction else None),
            )
        )
    for candidate in _STATE_PRIORITY:
        if candidate in states:
            return candidate
    return None


def set_subscription_access_state(
    db: Session,
    subscription_id: str,
    state: AccessState | None,
) -> AccessStateWriteResult:
    """Set ``subscription.access_state`` to ``state`` and mirror the
    SUBSCRIBER's aggregate state to external RADIUS ``radusergroup``.
    Idempotent.

    Two writes happen:

      1. ``subscription.access_state = state`` (per-sub column write).
         Reflects what this single subscription thinks its state should
         be. Used for observability/debugging.

      2. ``radusergroup`` row for every credential of the SUBSCRIBER
         is set to the group of the subscriber-aggregate state (see
         ``derive_subscriber_access_state``). Reflects the user's
         effective auth state, because credentials are per-subscriber
         and a subscriber with multiple subs in different states must
         get the most-permissive state's group (active > captive >
         suspended > terminated).

    The radusergroup write is the SHADOW path during phases 3-7 — the
    legacy block path still runs in parallel. Callers typically wrap
    this in a feature-flag check.

    The DELETE is scoped to ``groupname LIKE 'dotmac-%'`` so any
    operator-managed groups outside this namespace are preserved.

    Returns counts for observability:
      {"credentials": n, "external_rows_written": n,
       "external_rows_deleted": n, "aggregate_state": str | None}
    """
    sub = db.get(Subscription, coerce_uuid(subscription_id))
    if sub is None:
        return {
            "credentials": 0,
            "external_rows_written": 0,
            "external_rows_deleted": 0,
            "aggregate_state": None,
        }

    # 1. Per-sub column write
    new_value = state.value if state is not None else None
    if sub.access_state != new_value:
        sub.access_state = new_value
        db.flush()

    # 2. Subscriber-aggregate radusergroup write
    aggregate_state = derive_subscriber_access_state(db, sub.subscriber_id)

    credentials = list(
        db.scalars(
            select(AccessCredential).where(
                AccessCredential.subscriber_id == sub.subscriber_id
            )
        ).all()
    )
    if not credentials:
        return {
            "credentials": 0,
            "external_rows_written": 0,
            "external_rows_deleted": 0,
            "aggregate_state": aggregate_state.value if aggregate_state else None,
        }

    target_group: str | None = None
    if aggregate_state is not None and aggregate_state != AccessState.terminated:
        target_group = _GROUP_FOR_STATE[aggregate_state]

    external_configs = _active_external_sync_configs(db)
    if not external_configs:
        return {
            "credentials": len(credentials),
            "external_rows_written": 0,
            "external_rows_deleted": 0,
            "aggregate_state": aggregate_state.value if aggregate_state else None,
        }

    rows_written = 0
    rows_deleted = 0
    for config in external_configs:
        radusergroup = config.get("radusergroup_table", "radusergroup")
        try:
            engine = _get_external_engine(config["db_url"])
            radusergroup_table = _external_radius_table(
                radusergroup,
                Column("username", String),
                Column("groupname", String),
                Column("priority", Integer),
            )
            with engine.begin() as conn:
                for credential in credentials:
                    result = conn.execute(
                        delete(radusergroup_table).where(
                            radusergroup_table.c.username == credential.username,
                            radusergroup_table.c.groupname.like("dotmac-%"),
                        )
                    )
                    rows_deleted += result.rowcount or 0
                    if target_group:
                        conn.execute(
                            insert(radusergroup_table).values(
                                username=credential.username,
                                groupname=target_group,
                                priority=0,
                            )
                        )
                        rows_written += 1
        except Exception:
            logger.warning(
                "shadow set_subscription_access_state failed for sub=%s",
                subscription_id,
                exc_info=True,
            )

    return {
        "credentials": len(credentials),
        "external_rows_written": rows_written,
        "external_rows_deleted": rows_deleted,
        "aggregate_state": aggregate_state.value if aggregate_state else None,
    }
