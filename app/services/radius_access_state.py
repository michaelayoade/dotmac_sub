"""Derive and apply the RADIUS access state.

``derive_access_state`` — pure mapping (phase 2).
``set_subscription_access_state`` — dual-write app DB ``access_state``
+ external RADIUS ``radusergroup`` (phase 3, shadow).

See ``docs/radius_state_refactor/phase0_state_model.md``.
"""

from __future__ import annotations

import logging

from sqlalchemy import Column, Integer, String, delete, insert, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.services.common import coerce_uuid
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

logger = logging.getLogger(__name__)

# Status sets — declared here as constants so callers can also reason
# about which SubscriptionStatus values map to a given AccessState
# without inverting the function.

_ACTIVE_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.active,
})

_BLOCKED_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.suspended,
    SubscriptionStatus.blocked,
    SubscriptionStatus.stopped,
})

_TERMINATED_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.canceled,
    SubscriptionStatus.expired,
    SubscriptionStatus.disabled,
})

# Pending/hidden/archived → None. Not provisioned to RADIUS.
_UNPROVISIONED_STATUSES: frozenset[SubscriptionStatus] = frozenset({
    SubscriptionStatus.pending,
    SubscriptionStatus.hidden,
    SubscriptionStatus.archived,
})


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
    captive_redirect_enabled: bool,
) -> AccessState | None:
    """Pure mapping: subscription.status + captive flag → AccessState.

    Returns None when the subscription is not provisioned to RADIUS yet
    (pending, hidden, archived). Callers should treat None as "no
    radusergroup row should exist for this user".

    A blocked-status subscriber whose ``captive_redirect_enabled`` is
    set gets ``captive`` instead of ``suspended`` so they keep enough
    connectivity to reach the payment portal.
    """
    if subscription_status in _ACTIVE_STATUSES:
        return AccessState.active
    if subscription_status in _BLOCKED_STATUSES:
        if captive_redirect_enabled:
            return AccessState.captive
        return AccessState.suspended
    if subscription_status in _TERMINATED_STATUSES:
        return AccessState.terminated
    # Unprovisioned (pending/hidden/archived) or any future
    # SubscriptionStatus value we don't know yet → None.
    return None


def set_subscription_access_state(
    db: Session,
    subscription_id: str,
    state: AccessState | None,
) -> dict[str, int]:
    """Set ``subscription.access_state`` in the app DB AND mirror to
    external RADIUS ``radusergroup``. Idempotent.

    The radusergroup write is the SHADOW path during phases 3-7 — the
    legacy block path (IP rewrite + per-user radcheck/radreply + per-
    customer firewall address-list) still runs in parallel. Callers
    typically wrap this in a feature-flag check (see
    ``DomainSetting radius.group_routing_enabled``).

    Semantics per state:
      * active / suspended / captive — UPSERT one radusergroup row per
        credential with groupname = dotmac-<state>.
      * terminated — DELETE the user's dotmac-* radusergroup rows. No
        new row inserted; auth fails with user-not-found.
      * None — same as terminated (no row), used for unprovisioned subs.

    The DELETE is scoped to ``groupname LIKE 'dotmac-%'`` so any
    operator-managed groups outside this namespace are preserved.

    Returns counts for observability:
      {"credentials": n, "external_rows_written": n, "external_rows_deleted": n}
    """
    sub = db.get(Subscription, coerce_uuid(subscription_id))
    if sub is None:
        return {"credentials": 0, "external_rows_written": 0, "external_rows_deleted": 0}

    # 1. App DB
    new_value = state.value if state is not None else None
    if sub.access_state != new_value:
        sub.access_state = new_value
        db.flush()

    # 2. External RADIUS mirror
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
        }

    target_group: str | None = None
    if state is not None and state != AccessState.terminated:
        target_group = _GROUP_FOR_STATE[state]

    external_configs = _active_external_sync_configs(db)
    if not external_configs:
        return {
            "credentials": len(credentials),
            "external_rows_written": 0,
            "external_rows_deleted": 0,
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
    }
