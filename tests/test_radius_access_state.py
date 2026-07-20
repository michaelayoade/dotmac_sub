"""Pure mapping from status + owner-resolved restriction to AccessState."""

from __future__ import annotations

import pytest

from app.models.catalog import AccessState, SubscriptionStatus
from app.models.enforcement_lock import AccessRestrictionMode
from app.services.radius_access_state import derive_access_state


class TestDeriveAccessStateActive:
    def test_active_maps_to_active(self):
        assert derive_access_state(SubscriptionStatus.active) == AccessState.active

    def test_active_ignores_captive_restriction(self):
        assert (
            derive_access_state(
                SubscriptionStatus.active,
                restriction_mode=AccessRestrictionMode.captive,
            )
            == AccessState.active
        )


class TestDeriveAccessStateBlocked:
    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        ],
    )
    def test_owner_resolved_captive_maps_to_captive(self, status):
        assert (
            derive_access_state(
                status,
                restriction_mode=AccessRestrictionMode.captive,
            )
            == AccessState.captive
        )

    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        ],
    )
    def test_blocked_default_maps_to_suspended_hard_block(self, status):
        """Default (not opted in) → hard block (Auth-Type := Reject), NOT
        captive. The redirect is not applied to every account."""
        assert derive_access_state(status) == AccessState.suspended

    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.suspended,
            SubscriptionStatus.blocked,
            SubscriptionStatus.stopped,
        ],
    )
    def test_hard_reject_tier_maps_to_suspended(self, status):
        """Abuse/fraud tier is explicit opt-in via hard_reject."""
        assert derive_access_state(status, hard_reject=True) == AccessState.suspended


class TestDeriveAccessStateTerminated:
    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.canceled,
            SubscriptionStatus.expired,
            SubscriptionStatus.disabled,
        ],
    )
    def test_terminal_statuses_map_to_terminated(self, status):
        assert derive_access_state(status) == AccessState.terminated

    def test_terminal_ignores_captive_restriction(self):
        assert (
            derive_access_state(
                SubscriptionStatus.canceled,
                restriction_mode=AccessRestrictionMode.captive,
            )
            == AccessState.terminated
        )


class TestDeriveAccessStateUnprovisioned:
    @pytest.mark.parametrize(
        "status",
        [
            SubscriptionStatus.pending,
            SubscriptionStatus.hidden,
            SubscriptionStatus.archived,
        ],
    )
    def test_unprovisioned_statuses_map_to_none(self, status):
        assert derive_access_state(status) is None

    def test_unprovisioned_with_captive_restriction_still_none(self):
        """Captive restriction doesn't promote an unprovisioned sub —
        unprovisioned means literally not in RADIUS yet."""
        assert (
            derive_access_state(
                SubscriptionStatus.pending,
                restriction_mode=AccessRestrictionMode.captive,
            )
            is None
        )
