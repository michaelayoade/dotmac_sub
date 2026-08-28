"""Derive and apply the RADIUS access state.

``derive_access_state`` is the pure policy mapping. Persisted
``Subscription.access_state`` is written only by
``access.subscription_lifecycle``.

See ``docs/FINANCIAL_ACCESS_ENFORCEMENT.md``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
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


def stage_subscription_radius_profile(
    db: Session,
    *,
    subscription_id: UUID,
    credential_id: UUID,
    radius_profile_id: UUID | None,
) -> AccessCredential:
    """Stage one exact subscription credential's desired RADIUS override."""

    credential = db.get(AccessCredential, credential_id)
    if (
        credential is None
        or credential.subscription_id != subscription_id
        or not credential.is_active
    ):
        raise ValueError("Active credential does not belong to the subscription")
    credential.radius_profile_id = radius_profile_id
    db.flush()
    return credential


class CredentialProfileConflict(ValueError):
    """A credential is not in the RADIUS profile state the caller expected.

    A domain error, deliberately not an ``HTTPException``: this module is not
    in ``tests/architecture/service_http_exception_baseline.txt`` and must not
    be added to it. The adapter that owns a transport maps this to its own
    status code.
    """


def apply_throttle_profile(
    db: Session,
    *,
    credential_id: UUID,
    profile_before_id: UUID | None,
    throttle_profile_id: UUID,
) -> AccessCredential:
    """Move one credential onto a throttle profile, remembering what it left.

    ``pre_throttle_radius_profile_id`` is the restore anchor: it holds the
    profile the credential carried before the throttle, so a later restore
    does not have to re-derive it. A credential that was already on no profile
    leaves the anchor null, because there is nothing to come back to.

    An anchor that is already set is never overwritten. Re-throttling an
    already-throttled credential would otherwise record the throttle profile
    as the thing to restore, and the customer's real speed would be lost with
    no way to recover it from the row.
    """

    credential = db.get(AccessCredential, credential_id)
    if credential is None or credential.radius_profile_id != profile_before_id:
        raise CredentialProfileConflict(
            "Access credential is not on the profile the throttle expected"
        )
    if (
        credential.radius_profile_id is not None
        and credential.pre_throttle_radius_profile_id is None
    ):
        credential.pre_throttle_radius_profile_id = credential.radius_profile_id
    credential.radius_profile_id = throttle_profile_id
    db.flush()
    return credential


def restore_throttle_profile(
    db: Session,
    *,
    credential_id: UUID,
    throttled_profile_id: UUID | None,
    restore_profile_id: UUID | None,
) -> AccessCredential:
    """Return one throttled credential to its remembered profile.

    Both halves are checked, not just the current profile: a credential whose
    restore anchor no longer matches is one some other owner has moved since
    the throttle, and guessing which profile it should end on would be
    inventing a decision this module does not own.
    """

    credential = db.get(AccessCredential, credential_id)
    if (
        credential is None
        or credential.radius_profile_id != throttled_profile_id
        or credential.pre_throttle_radius_profile_id != restore_profile_id
    ):
        raise CredentialProfileConflict(
            "Throttled credential is not in the state the restore expected"
        )
    credential.radius_profile_id = restore_profile_id
    credential.pre_throttle_radius_profile_id = None
    db.flush()
    return credential


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
        SubscriptionStatus.disabled,
    }
)

_TERMINATED_STATUSES: frozenset[SubscriptionStatus] = frozenset(
    {
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
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
        if restriction_mode == AccessRestrictionMode.captive:
            return AccessState.captive
        if restriction_mode == AccessRestrictionMode.hard_reject or hard_reject:
            return AccessState.suspended
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
