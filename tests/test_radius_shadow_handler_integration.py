"""Phase 4 — integration tests for the EnforcementHandler shadow-write
wiring. Confirms that _enforce_subscription_block and
_handle_subscription_restore actually call _shadow_write_access_state,
and that the feature flag correctly gates the call.

These tests don't exercise the full block path (that's covered
elsewhere) — they specifically verify the new phase-3 wiring.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.catalog import (
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.services.events.handlers.enforcement import EnforcementHandler
from app.services.events.types import Event, EventType


def _stub_subscription(*, status=SubscriptionStatus.active, subscriber_id=None):
    sub = MagicMock(spec=Subscription)
    sub.id = uuid4()
    sub.subscriber_id = subscriber_id or uuid4()
    sub.status = status
    return sub


def _stub_subscriber(*, captive=False):
    subscriber = MagicMock()
    subscriber.captive_redirect_enabled = captive
    return subscriber


class TestShadowWriteFeatureFlagGate:
    def test_flag_off_updates_local_state_without_external_set_call(self):
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription(status=SubscriptionStatus.suspended)
        sub.access_state = None
        subscriber = _stub_subscriber(captive=False)
        db.get.side_effect = [sub, subscriber]

        with (
            patch(
                "app.services.events.handlers.enforcement._setting_bool",
                return_value=False,
            ),
            patch(
                "app.services.events.handlers.enforcement.set_subscription_access_state"
            ) as mock_set,
        ):
            handler._shadow_write_access_state(db, str(sub.id))

        mock_set.assert_not_called()
        assert sub.access_state == AccessState.suspended.value
        db.flush.assert_called_once()

    def test_flag_on_calls_set_with_derived_state(self):
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription(status=SubscriptionStatus.suspended)
        subscriber = _stub_subscriber(captive=False)
        # First db.get returns the subscription, second returns the subscriber.
        db.get.side_effect = [sub, subscriber]

        with (
            patch(
                "app.services.events.handlers.enforcement._setting_bool",
                return_value=True,
            ),
            patch(
                "app.services.events.handlers.enforcement.set_subscription_access_state",
                return_value={
                    "credentials": 1,
                    "external_rows_written": 1,
                    "external_rows_deleted": 0,
                },
            ) as mock_set,
        ):
            handler._shadow_write_access_state(db, str(sub.id))

        # Captive redirect is opt-in: with the flag off, a suspended sub
        # derives to suspended (hard reject), not captive.
        mock_set.assert_called_once_with(db, str(sub.id), AccessState.suspended)

    def test_flag_on_captive_subscriber_routes_to_captive(self):
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription(status=SubscriptionStatus.suspended)
        subscriber = _stub_subscriber(captive=True)
        db.get.side_effect = [sub, subscriber]

        with (
            patch(
                "app.services.events.handlers.enforcement._setting_bool",
                return_value=True,
            ),
            patch(
                "app.services.events.handlers.enforcement.set_subscription_access_state",
                return_value={
                    "credentials": 1,
                    "external_rows_written": 1,
                    "external_rows_deleted": 0,
                },
            ) as mock_set,
        ):
            handler._shadow_write_access_state(db, str(sub.id))

        mock_set.assert_called_once_with(db, str(sub.id), AccessState.captive)

    def test_set_failure_is_swallowed_not_raised(self):
        """A failing shadow write must not break the caller — the legacy
        block path is still authoritative."""
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription()
        subscriber = _stub_subscriber()
        db.get.side_effect = [sub, subscriber]

        with (
            patch(
                "app.services.events.handlers.enforcement._setting_bool",
                return_value=True,
            ),
            patch(
                "app.services.events.handlers.enforcement.set_subscription_access_state",
                side_effect=RuntimeError("external DB down"),
            ),
        ):
            handler._shadow_write_access_state(db, str(sub.id))  # must not raise

    def test_missing_subscription_returns_without_calling_set(self):
        handler = EnforcementHandler()
        db = MagicMock()
        db.get.return_value = None

        with (
            patch(
                "app.services.events.handlers.enforcement._setting_bool",
                return_value=True,
            ),
            patch(
                "app.services.events.handlers.enforcement.set_subscription_access_state"
            ) as mock_set,
        ):
            handler._shadow_write_access_state(db, str(uuid4()))

        mock_set.assert_not_called()


class TestBlockHandlerInvokesShadowWrite:
    """Confirms _enforce_subscription_block calls _shadow_write_access_state
    once at the end of its sequence."""

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch("app.services.events.handlers.enforcement.radius_service")
    @patch("app.services.events.handlers.enforcement.radius_reject_service")
    def test_block_path_calls_shadow_write(
        self,
        _mock_reject,
        _mock_radius,
        _mock_cleanup,
    ):
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription()
        db.get.return_value = sub

        with patch.object(handler, "_shadow_write_access_state") as mock_shadow:
            handler._enforce_subscription_block(db, str(sub.id))

        mock_shadow.assert_called_once_with(db, str(sub.id))


class TestRestoreHandlerInvokesShadowWrite:
    """Confirms _handle_subscription_restore calls _shadow_write_access_state
    after the reconcile step."""

    @patch(
        "app.services.events.handlers.enforcement.remove_subscription_address_list_block"
    )
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.events.handlers.enforcement.radius_service")
    @patch("app.services.events.handlers.enforcement.radius_reject_service")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_restore_path_calls_shadow_write(
        self,
        mock_settings,
        _mock_reject,
        _mock_radius,
        _mock_disconnect,
        _mock_remove_list,
    ):
        handler = EnforcementHandler()
        db = MagicMock()
        sub = _stub_subscription()
        db.get.return_value = sub
        mock_settings.resolve_value.return_value = "true"
        event = Event(
            event_type=EventType.subscription_resumed,
            payload={"subscription_id": str(sub.id)},
            subscription_id=sub.id,
        )

        with (
            patch.object(handler, "_shadow_write_access_state") as mock_shadow,
            patch("app.services.account_lifecycle.compute_account_status"),
        ):
            handler._handle_subscription_restore(db, event)

        mock_shadow.assert_called_once_with(db, str(sub.id))
