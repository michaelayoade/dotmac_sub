"""Derive the RADIUS access state from subscription + subscriber inputs.

Phase 2 of the access-state refactor — this module currently only exposes
the pure ``derive_access_state`` function. Phase 3 will add
``set_subscription_access_state`` that wraps the dual-write to
``subscription.access_state`` + external RADIUS ``radusergroup``.

See ``docs/radius_state_refactor/phase0_state_model.md``.
"""

from __future__ import annotations

from app.models.catalog import AccessState, SubscriptionStatus

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
