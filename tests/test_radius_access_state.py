"""Tests for derive_access_state — pure function mapping
SubscriptionStatus + captive flag → AccessState. Phase 2.
"""

from __future__ import annotations

import pytest

from app.models.catalog import AccessState, SubscriptionStatus
from app.services.radius_access_state import derive_access_state


class TestDeriveAccessStateActive:
    def test_active_maps_to_active(self):
        assert (
            derive_access_state(
                SubscriptionStatus.active, captive_redirect_enabled=False
            )
            == AccessState.active
        )

    def test_active_ignores_captive_flag(self):
        """Active subscribers don't get routed to captive even if their
        captive_redirect_enabled flag is set — captive is for blocked
        subscribers only."""
        assert (
            derive_access_state(
                SubscriptionStatus.active, captive_redirect_enabled=True
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
    def test_blocked_opt_in_maps_to_captive(self, status):
        """Captive redirect is per-customer opt-in: only an opted-in blocked
        subscriber gets the soft walled-garden."""
        assert (
            derive_access_state(status, captive_redirect_enabled=True)
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
        assert (
            derive_access_state(status, captive_redirect_enabled=False)
            == AccessState.suspended
        )

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
        assert (
            derive_access_state(status, captive_redirect_enabled=False)
            == AccessState.terminated
        )

    def test_terminal_ignores_captive_flag(self):
        """A terminated subscriber doesn't get captive routing even if
        they're flagged for it. Terminated overrides."""
        assert (
            derive_access_state(
                SubscriptionStatus.canceled, captive_redirect_enabled=True
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
        assert derive_access_state(status, captive_redirect_enabled=False) is None

    def test_unprovisioned_with_captive_flag_still_none(self):
        """Captive flag doesn't promote an unprovisioned sub to captive —
        unprovisioned means literally not in RADIUS yet."""
        assert (
            derive_access_state(
                SubscriptionStatus.pending, captive_redirect_enabled=True
            )
            is None
        )
