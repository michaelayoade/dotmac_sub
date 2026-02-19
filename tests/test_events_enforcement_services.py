"""Tests for the event system, enforcement service, and provisioning adapters.

Covers:
- Event types enum/constants
- Event dataclass serialization
- Event dispatcher: register_handler, dispatch, retry_event
- Enforcement handler: subscription block/restore, throttle, usage exhausted
- Lifecycle handler: subscription lifecycle events
- Notification handler: template lookup and notification creation
- Webhook handler: delivery creation and queuing
- Provisioning handler: auto IP allocation, service order provisioning
- Provisioning adapters: StubProvisioner, register/get provisioner
- Enforcement service helpers: _setting_bool, address list operations
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.catalog import NasDevice, NasVendor, SubscriptionStatus
from app.models.provisioning import ProvisioningVendor
from app.services.events.types import (
    Event,
    EventType,
    SUBSCRIPTION_LIFECYCLE_MAP,
)
from app.services.events.dispatcher import EventDispatcher
from app.services.events.handlers.enforcement import EnforcementHandler
from app.services.events.handlers.lifecycle import LifecycleHandler
from app.services.events.handlers.notification import (
    EVENT_TYPE_TO_TEMPLATE,
    NotificationHandler,
)
from app.services.events.handlers.provisioning import ProvisioningHandler
from app.services.events.handlers.webhook import (
    EVENT_TYPE_TO_WEBHOOK,
    WebhookHandler,
)
from app.services.provisioning_adapters import (
    ProvisioningResult,
    StubProvisioner,
    get_provisioner,
    register_provisioner,
    _resolve_connection,
)


# ---------------------------------------------------------------------------
# EventType enum tests
# ---------------------------------------------------------------------------

class TestEventType:
    def test_subscriber_events_exist(self):
        assert EventType.subscriber_created.value == "subscriber.created"
        assert EventType.subscriber_updated.value == "subscriber.updated"
        assert EventType.subscriber_suspended.value == "subscriber.suspended"
        assert EventType.subscriber_reactivated.value == "subscriber.reactivated"
        assert EventType.subscriber_throttled.value == "subscriber.throttled"

    def test_subscription_events_exist(self):
        assert EventType.subscription_created.value == "subscription.created"
        assert EventType.subscription_activated.value == "subscription.activated"
        assert EventType.subscription_suspended.value == "subscription.suspended"
        assert EventType.subscription_resumed.value == "subscription.resumed"
        assert EventType.subscription_canceled.value == "subscription.canceled"
        assert EventType.subscription_upgraded.value == "subscription.upgraded"
        assert EventType.subscription_downgraded.value == "subscription.downgraded"
        assert EventType.subscription_expiring.value == "subscription.expiring"
        assert EventType.subscription_expired.value == "subscription.expired"

    def test_billing_events_exist(self):
        assert EventType.invoice_created.value == "invoice.created"
        assert EventType.invoice_paid.value == "invoice.paid"
        assert EventType.payment_received.value == "payment.received"
        assert EventType.payment_failed.value == "payment.failed"

    def test_usage_events_exist(self):
        assert EventType.usage_recorded.value == "usage.recorded"
        assert EventType.usage_warning.value == "usage.warning"
        assert EventType.usage_exhausted.value == "usage.exhausted"
        assert EventType.usage_topped_up.value == "usage.topped_up"

    def test_provisioning_events_exist(self):
        assert EventType.provisioning_started.value == "provisioning.started"
        assert EventType.provisioning_completed.value == "provisioning.completed"
        assert EventType.provisioning_failed.value == "provisioning.failed"

    def test_network_events_exist(self):
        assert EventType.device_offline.value == "device.offline"
        assert EventType.device_online.value == "device.online"

    def test_custom_event_type(self):
        assert EventType.custom.value == "custom"

    def test_event_type_from_value(self):
        assert EventType("subscriber.created") == EventType.subscriber_created

    def test_event_type_invalid_value_raises(self):
        with pytest.raises(ValueError):
            EventType("nonexistent.event")

    def test_subscription_lifecycle_map_keys(self):
        expected_keys = {
            EventType.subscription_activated,
            EventType.subscription_suspended,
            EventType.subscription_resumed,
            EventType.subscription_canceled,
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
        }
        assert set(SUBSCRIPTION_LIFECYCLE_MAP.keys()) == expected_keys

    def test_subscription_lifecycle_map_values(self):
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_activated] == "activate"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_suspended] == "suspend"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_resumed] == "resume"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_canceled] == "cancel"


# ---------------------------------------------------------------------------
# Event dataclass tests
# ---------------------------------------------------------------------------

class TestEventDataclass:
    def test_event_defaults(self):
        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        assert event.event_type == EventType.subscriber_created
        assert event.payload == {"name": "Test"}
        assert isinstance(event.event_id, uuid.UUID)
        assert isinstance(event.occurred_at, datetime)
        assert event.actor is None
        assert event.subscriber_id is None

    def test_event_with_context(self):
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"plan": "basic"},
            actor="admin@example.com",
            subscriber_id=sub_id,
            account_id=acc_id,
        )
        assert event.actor == "admin@example.com"
        assert event.subscriber_id == sub_id
        assert event.account_id == acc_id

    def test_event_to_dict(self):
        event_id = uuid.uuid4()
        sub_id = uuid.uuid4()
        event = Event(
            event_type=EventType.invoice_created,
            payload={"amount": 100, "nested": {"id": uuid.uuid4()}},
            event_id=event_id,
            subscriber_id=sub_id,
            actor="system",
        )
        result = event.to_dict()
        assert result["event_id"] == str(event_id)
        assert result["event_type"] == "invoice.created"
        assert isinstance(result["occurred_at"], str)
        assert result["payload"]["amount"] == 100
        # Nested UUIDs should be serialized
        assert isinstance(result["payload"]["nested"]["id"], str)
        assert result["context"]["actor"] == "system"
        assert result["context"]["subscriber_id"] == str(sub_id)
        assert result["context"]["account_id"] is None

    def test_event_to_dict_with_list_payload(self):
        event = Event(
            event_type=EventType.custom,
            payload={"items": [uuid.uuid4(), uuid.uuid4()]},
        )
        result = event.to_dict()
        for item in result["payload"]["items"]:
            assert isinstance(item, str)


# ---------------------------------------------------------------------------
# EventDispatcher tests
# ---------------------------------------------------------------------------

class TestEventDispatcher:
    def test_register_handler(self):
        dispatcher = EventDispatcher()
        handler = MagicMock()
        dispatcher.register_handler(handler)
        assert handler in dispatcher._handlers

    def test_register_multiple_handlers(self):
        dispatcher = EventDispatcher()
        h1, h2, h3 = MagicMock(), MagicMock(), MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)
        dispatcher.register_handler(h3)
        assert len(dispatcher._handlers) == 3

    def test_dispatch_calls_all_handlers(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h2 = MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        mock_db = MagicMock()
        dispatcher.dispatch(mock_db, event)

        h1.handle.assert_called_once_with(mock_db, event)
        h2.handle.assert_called_once_with(mock_db, event)

    def test_dispatch_handler_failure_does_not_stop_others(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.handle.side_effect = RuntimeError("handler 1 failed")
        h1.__class__.__name__ = "FailingHandler"
        h2 = MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        event = Event(
            event_type=EventType.subscriber_created,
            payload={},
        )
        mock_db = MagicMock()
        # Should not raise even though h1 fails
        dispatcher.dispatch(mock_db, event)

        # h2 should still be called
        h2.handle.assert_called_once_with(mock_db, event)

    def test_dispatch_persists_event_record(self, db_session):
        dispatcher = EventDispatcher()
        event = Event(
            event_type=EventType.payment_received,
            payload={"amount": 50},
        )
        mock_db = MagicMock()
        dispatcher.dispatch(mock_db, event)
        # Verify db.add was called (event persistence)
        mock_db.add.assert_called_once()

    def test_retry_event_calls_only_failed_handlers(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.__class__ = type("SuccessHandler", (), {"__name__": "SuccessHandler", "handle": h1.handle})
        h1.__class__.__name__ = "SuccessHandler"
        h2 = MagicMock()
        h2.__class__ = type("FailedHandler", (), {"__name__": "FailedHandler", "handle": h2.handle})
        h2.__class__.__name__ = "FailedHandler"
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        # Simulate a stored event record with a failure
        event_record = MagicMock()
        event_record.event_id = uuid.uuid4()
        event_record.event_type = "subscriber.created"
        event_record.payload = {"name": "retry test"}
        event_record.actor = None
        event_record.subscriber_id = None
        event_record.account_id = None
        event_record.subscription_id = None
        event_record.invoice_id = None
        event_record.service_order_id = None
        event_record.failed_handlers = [{"handler": "FailedHandler", "error": "boom"}]
        event_record.retry_count = 0

        mock_db = MagicMock()
        result = dispatcher.retry_event(mock_db, event_record)

        # Only h2 (FailedHandler) should be retried, not h1
        h1.handle.assert_not_called()
        h2.handle.assert_called_once()
        assert result is True

    def test_retry_event_returns_false_on_failure(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.__class__.__name__ = "FailingHandler"
        h1.handle.side_effect = RuntimeError("still broken")
        dispatcher.register_handler(h1)

        event_record = MagicMock()
        event_record.event_id = uuid.uuid4()
        event_record.event_type = "subscriber.created"
        event_record.payload = {}
        event_record.actor = None
        event_record.subscriber_id = None
        event_record.account_id = None
        event_record.subscription_id = None
        event_record.invoice_id = None
        event_record.service_order_id = None
        event_record.failed_handlers = [{"handler": "FailingHandler", "error": "boom"}]
        event_record.retry_count = 0

        mock_db = MagicMock()
        result = dispatcher.retry_event(mock_db, event_record)

        assert result is False


# ---------------------------------------------------------------------------
# EnforcementHandler tests
# ---------------------------------------------------------------------------

class TestEnforcementHandler:
    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    @patch("app.services.events.handlers.enforcement.apply_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    def test_subscription_suspended_disconnects_and_blocks(
        self, mock_disconnect, mock_block, db_session
    ):
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_suspended,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)

        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="suspended")
        mock_block.assert_called_once_with(db_session, str(sub_id))

    @patch("app.services.events.handlers.enforcement.apply_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    def test_subscription_canceled_disconnects_and_blocks(
        self, mock_disconnect, mock_block, db_session
    ):
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_canceled,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)

        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="canceled")
        mock_block.assert_called_once_with(db_session, str(sub_id))

    @patch("app.services.events.handlers.enforcement.apply_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    def test_subscription_block_uses_payload_fallback(
        self, mock_disconnect, mock_block, db_session
    ):
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_suspended,
            payload={"subscription_id": str(sub_id)},
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="suspended")

    @patch("app.services.events.handlers.enforcement.apply_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    def test_subscription_block_skips_without_id(
        self, mock_disconnect, mock_block, db_session
    ):
        handler = EnforcementHandler()
        event = self._make_event(EventType.subscription_suspended)
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()
        mock_block.assert_not_called()

    @patch("app.services.events.handlers.enforcement.remove_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscription_activated_restores(
        self, mock_settings, mock_disconnect, mock_remove_block, db_session
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="restore")
        mock_remove_block.assert_called_once_with(db_session, str(sub_id))

    @patch("app.services.events.handlers.enforcement.remove_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscription_resumed_restores(
        self, mock_settings, mock_disconnect, mock_remove_block, db_session
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_resumed,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="restore")
        mock_remove_block.assert_called_once_with(db_session, str(sub_id))

    @patch("app.services.events.handlers.enforcement.remove_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscription_restore_skips_disconnect_when_refresh_disabled(
        self, mock_settings, mock_disconnect, mock_remove_block, db_session
    ):
        mock_settings.resolve_value.return_value = "false"
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()
        mock_remove_block.assert_called_once()

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscriber_throttled_disconnects_sessions(
        self, mock_settings, mock_disconnect, db_session
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscriber_throttled,
            account_id=acc_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(db_session, str(acc_id), reason="throttle")

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscriber_throttled_skips_when_refresh_disabled(
        self, mock_settings, mock_disconnect, db_session
    ):
        mock_settings.resolve_value.return_value = "off"
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscriber_throttled,
            account_id=uuid.uuid4(),
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_subscriber_throttled_skips_without_account_id(
        self, mock_settings, mock_disconnect, db_session
    ):
        handler = EnforcementHandler()
        event = self._make_event(EventType.subscriber_throttled)
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()

    @patch("app.services.events.handlers.enforcement.apply_subscription_address_list_block")
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_block_action(
        self, mock_settings, mock_disconnect, mock_block, db_session
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "block"
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(db_session, str(sub_id), reason="fup_block")
        mock_block.assert_called_once_with(db_session, str(sub_id))

    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_none_action_is_noop(self, mock_settings, db_session):
        mock_settings.resolve_value.return_value = "none"
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        # Should not raise or do anything
        handler.handle(db_session, event)

    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_skips_without_ids(self, mock_settings, db_session):
        handler = EnforcementHandler()
        event = self._make_event(EventType.usage_exhausted)
        handler.handle(db_session, event)
        mock_settings.resolve_value.assert_not_called()

    @patch("app.services.events.handlers.enforcement.emit_event")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_suspend_action(
        self, mock_settings, mock_emit, db_session, subscription
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "suspend"
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect

        # Set subscription to active
        subscription.status = SubscriptionStatus.active
        db_session.flush()

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        handler.handle(db_session, event)

        # Should have updated status
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended
        # Should have emitted a subscription_suspended event
        mock_emit.assert_called_once()
        call_args = mock_emit.call_args
        assert call_args[0][1] == EventType.subscription_suspended

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.apply_radius_profile_to_account")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_throttle_action(
        self, mock_settings, mock_apply_profile, mock_disconnect, db_session
    ):
        throttle_profile_id = uuid.uuid4()

        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "throttle"
            if key == "fup_throttle_radius_profile_id":
                return str(throttle_profile_id)
            if key == "refresh_sessions_on_profile_change":
                return "true"
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        mock_apply_profile.return_value = 1

        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        handler.handle(db_session, event)

        mock_apply_profile.assert_called_once_with(
            db_session, str(acc_id), str(throttle_profile_id)
        )
        mock_disconnect.assert_called_once_with(db_session, str(acc_id), reason="fup_throttle")

    @patch("app.services.events.handlers.enforcement.apply_radius_profile_to_account")
    @patch("app.services.events.handlers.enforcement.settings_spec")
    def test_usage_exhausted_throttle_without_profile_is_noop(
        self, mock_settings, mock_apply_profile, db_session
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "throttle"
            if key == "fup_throttle_radius_profile_id":
                return None
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect

        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        handler.handle(db_session, event)
        mock_apply_profile.assert_not_called()

    def test_enforcement_handler_ignores_unrelated_events(self, db_session):
        handler = EnforcementHandler()
        event = self._make_event(EventType.invoice_created)
        # Should simply return without error
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# LifecycleHandler tests
# ---------------------------------------------------------------------------

class TestLifecycleHandler:
    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    def test_lifecycle_records_activate_event(self, db_session, subscription):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_activated,
            payload={
                "from_status": "pending",
                "to_status": "active",
                "reason": "payment received",
            },
            subscription_id=subscription.id,
            actor="admin",
        )
        handler.handle(db_session, event)
        db_session.flush()

        from app.models.lifecycle import SubscriptionLifecycleEvent

        records = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .all()
        )
        assert len(records) >= 1
        record = records[-1]
        assert record.event_type.value == "activate"
        assert record.from_status == SubscriptionStatus.pending
        assert record.to_status == SubscriptionStatus.active
        assert record.reason == "payment received"
        assert record.actor == "admin"

    def test_lifecycle_records_suspend_event(self, db_session, subscription):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_suspended,
            payload={
                "from_status": "active",
                "to_status": "suspended",
            },
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        db_session.flush()

        from app.models.lifecycle import SubscriptionLifecycleEvent

        records = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .all()
        )
        assert len(records) >= 1
        assert records[-1].event_type.value == "suspend"

    def test_lifecycle_ignores_non_subscription_events(self, db_session):
        handler = LifecycleHandler()
        event = self._make_event(EventType.invoice_created, payload={})
        # Should not create any records
        handler.handle(db_session, event)

    def test_lifecycle_skips_when_no_subscription_id(self, db_session):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_activated,
            payload={"from_status": "pending", "to_status": "active"},
            # No subscription_id
        )
        # Should log warning but not raise
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# NotificationHandler tests
# ---------------------------------------------------------------------------

class TestNotificationHandler:
    def test_event_type_to_template_mapping_exists(self):
        assert EventType.subscription_created in EVENT_TYPE_TO_TEMPLATE
        assert EventType.invoice_created in EVENT_TYPE_TO_TEMPLATE
        assert EventType.payment_received in EVENT_TYPE_TO_TEMPLATE
        assert EventType.usage_warning in EVENT_TYPE_TO_TEMPLATE

    def test_notification_handler_ignores_unmapped_events(self, db_session):
        handler = NotificationHandler()
        event = Event(
            event_type=EventType.device_offline,
            payload={},
        )
        # Should not raise
        handler.handle(db_session, event)

    def test_notification_handler_ignores_when_no_template(self, db_session):
        handler = NotificationHandler()
        event = Event(
            event_type=EventType.subscription_created,
            payload={},
        )
        # No template in DB, should silently return
        handler.handle(db_session, event)

    def test_render_subject_with_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.subject = "Your {plan_name} subscription is ready"
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"plan_name": "Gold"},
        )
        result = handler._render_subject(template, event)
        assert result == "Your Gold subscription is ready"

    def test_render_subject_without_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.subject = None
        event = Event(
            event_type=EventType.subscription_activated,
            payload={},
        )
        result = handler._render_subject(template, event)
        assert "subscription.activated" in result

    def test_render_body_with_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.body = "Dear customer, your invoice #{invoice_number} is ready."
        event = Event(
            event_type=EventType.invoice_created,
            payload={"invoice_number": "INV-001"},
        )
        result = handler._render_body(template, event)
        assert "INV-001" in result

    def test_render_body_without_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.body = None
        event = Event(
            event_type=EventType.invoice_created,
            payload={"amount": 100},
        )
        result = handler._render_body(template, event)
        assert "invoice.created" in result


# ---------------------------------------------------------------------------
# WebhookHandler tests
# ---------------------------------------------------------------------------

class TestWebhookHandler:
    def test_event_type_to_webhook_mapping_completeness(self):
        # Most event types should have a webhook mapping
        assert EventType.subscriber_created in EVENT_TYPE_TO_WEBHOOK
        assert EventType.subscription_activated in EVENT_TYPE_TO_WEBHOOK
        assert EventType.invoice_paid in EVENT_TYPE_TO_WEBHOOK
        assert EventType.payment_received in EVENT_TYPE_TO_WEBHOOK
        assert EventType.provisioning_completed in EVENT_TYPE_TO_WEBHOOK
        assert EventType.custom in EVENT_TYPE_TO_WEBHOOK

    def test_webhook_handler_ignores_unmapped_events(self, db_session):
        handler = WebhookHandler()
        # dunning events are not in the mapping
        event = Event(
            event_type=EventType.dunning_started,
            payload={},
        )
        handler.handle(db_session, event)

    def test_webhook_handler_no_subscriptions(self, db_session):
        handler = WebhookHandler()
        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        # No webhook subscriptions in DB - should silently return
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# ProvisioningHandler tests
# ---------------------------------------------------------------------------

class TestProvisioningHandler:
    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_triggers_ip_allocation(
        self, mock_prov_svc, db_session
    ):
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_called_once_with(
            db_session, str(sub_id)
        )

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_uses_payload_fallback(
        self, mock_prov_svc, db_session
    ):
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            payload={"subscription_id": str(sub_id)},
        )
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_called_once_with(
            db_session, str(sub_id)
        )

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_skips_without_id(self, mock_prov_svc, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.subscription_activated)
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_not_called()

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_handles_failure_gracefully(
        self, mock_prov_svc, db_session
    ):
        mock_prov_svc.ensure_ip_assignments_for_subscription.side_effect = RuntimeError(
            "IP pool exhausted"
        )
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        # Should not raise
        handler.handle(db_session, event)

    def test_provisioning_handler_ignores_unrelated_events(self, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.invoice_created)
        handler.handle(db_session, event)

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_service_order_assigned_skips_without_id(self, mock_prov_svc, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.service_order_assigned)
        handler.handle(db_session, event)
        mock_prov_svc.resolve_workflow_for_service_order.assert_not_called()


# ---------------------------------------------------------------------------
# Provisioning Adapters tests
# ---------------------------------------------------------------------------

class TestProvisioningAdapters:
    def test_stub_provisioner_assign_ont(self):
        stub = StubProvisioner(ProvisioningVendor.other)
        result = stub.assign_ont({}, {"key": "value"})
        assert isinstance(result, ProvisioningResult)
        assert result.status == "ok"
        assert result.detail == "assign_ont stub"
        assert result.payload == {"key": "value"}

    def test_stub_provisioner_push_config(self):
        stub = StubProvisioner(ProvisioningVendor.other)
        result = stub.push_config({}, None)
        assert result.status == "ok"
        assert result.detail == "push_config stub"

    def test_stub_provisioner_confirm_up(self):
        stub = StubProvisioner(ProvisioningVendor.other)
        result = stub.confirm_up({"ctx": True}, None)
        assert result.status == "ok"
        assert result.detail == "confirm_up stub"

    def test_get_provisioner_returns_registered(self):
        stub = StubProvisioner(ProvisioningVendor.nokia)
        register_provisioner(stub)
        result = get_provisioner(ProvisioningVendor.nokia)
        assert result is stub

    def test_get_provisioner_returns_stub_for_unknown(self):
        # Use a vendor that we intentionally de-register
        from app.services.provisioning_adapters import _PROVISIONERS
        # Nokia is registered above, but "other" may not be
        original = _PROVISIONERS.pop(ProvisioningVendor.other, None)
        try:
            provisioner = get_provisioner(ProvisioningVendor.other)
            assert isinstance(provisioner, StubProvisioner)
            assert provisioner.vendor == ProvisioningVendor.other
        finally:
            if original is not None:
                _PROVISIONERS[ProvisioningVendor.other] = original

    def test_get_provisioner_mikrotik(self):
        from app.services.provisioning_adapters import MikrotikProvisioner
        provisioner = get_provisioner(ProvisioningVendor.mikrotik)
        assert isinstance(provisioner, MikrotikProvisioner)

    def test_get_provisioner_huawei(self):
        from app.services.provisioning_adapters import HuaweiProvisioner
        provisioner = get_provisioner(ProvisioningVendor.huawei)
        assert isinstance(provisioner, HuaweiProvisioner)

    def test_get_provisioner_zte_is_huawei_subclass(self):
        from app.services.provisioning_adapters import ZteProvisioner
        provisioner = get_provisioner(ProvisioningVendor.zte)
        assert isinstance(provisioner, ZteProvisioner)

    def test_get_provisioner_genieacs(self):
        from app.services.provisioning_adapters import GenieACSProvisioner
        provisioner = get_provisioner(ProvisioningVendor.genieacs)
        assert isinstance(provisioner, GenieACSProvisioner)

    def test_resolve_connection_from_connector(self):
        context = {
            "connector": {
                "base_url": "https://router.local:8728",
                "auth_config": {
                    "username": "admin",
                    "password": "secret",
                },
            },
        }
        conn = _resolve_connection(context)
        assert conn["host"] == "router.local"
        assert conn["port"] == 8728
        assert conn["username"] == "admin"
        assert conn["password"] == "secret"

    def test_resolve_connection_empty_context(self):
        conn = _resolve_connection({})
        assert conn["host"] is None
        assert conn["username"] is None

    def test_resolve_connection_explicit_host(self):
        context = {
            "connector": {
                "auth_config": {
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "root",
                },
            },
        }
        conn = _resolve_connection(context)
        assert conn["host"] == "10.0.0.1"
        assert conn["port"] == 22

    def test_provisioning_result_fields(self):
        result = ProvisioningResult(
            status="ok",
            detail="test",
            payload={"key": "value"},
        )
        assert result.status == "ok"
        assert result.detail == "test"
        assert result.payload == {"key": "value"}

    def test_provisioning_result_defaults(self):
        result = ProvisioningResult(status="error")
        assert result.detail is None
        assert result.payload is None


# ---------------------------------------------------------------------------
# Enforcement service helper tests
# ---------------------------------------------------------------------------

class TestEnforcementServiceHelpers:
    def test_apply_mikrotik_address_list_add(self, monkeypatch):
        commands: list[str] = []

        def _fake_ssh(_device, command):
            commands.append(command)

        monkeypatch.setattr(
            "app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_ssh
        )
        from app.services.enforcement import _apply_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.mikrotik)
        result = _apply_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is True
        assert len(commands) == 1
        assert "add" in commands[0]
        assert "10.0.0.1" in commands[0]

    def test_apply_mikrotik_address_list_wrong_vendor(self):
        from app.services.enforcement import _apply_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.huawei)
        result = _apply_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is False

    def test_remove_mikrotik_address_list(self, monkeypatch):
        commands: list[str] = []

        def _fake_ssh(_device, command):
            commands.append(command)

        monkeypatch.setattr(
            "app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_ssh
        )
        from app.services.enforcement import _remove_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.mikrotik)
        result = _remove_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is True
        assert "remove" in commands[0]

    def test_remove_mikrotik_address_list_wrong_vendor(self):
        from app.services.enforcement import _remove_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.cisco)
        result = _remove_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is False

    def test_setting_bool_true_values(self, monkeypatch):
        from app.services.enforcement import _setting_bool
        from app.models.domain_settings import SettingDomain

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: "true",
        )
        assert _setting_bool(None, SettingDomain.radius, "test", False) is True

    def test_setting_bool_false_values(self, monkeypatch):
        from app.services.enforcement import _setting_bool
        from app.models.domain_settings import SettingDomain

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: "no",
        )
        assert _setting_bool(None, SettingDomain.radius, "test", True) is False

    def test_setting_bool_none_uses_default(self, monkeypatch):
        from app.services.enforcement import _setting_bool
        from app.models.domain_settings import SettingDomain

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: None,
        )
        assert _setting_bool(None, SettingDomain.radius, "test", True) is True
        assert _setting_bool(None, SettingDomain.radius, "test", False) is False

    def test_setting_bool_with_actual_bool(self, monkeypatch):
        from app.services.enforcement import _setting_bool
        from app.models.domain_settings import SettingDomain

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: True,
        )
        assert _setting_bool(None, SettingDomain.radius, "test", False) is True


# ---------------------------------------------------------------------------
# emit_event integration test
# ---------------------------------------------------------------------------

class TestEmitEvent:
    @patch("app.services.events.dispatcher._dispatcher", None)
    @patch("app.services.events.dispatcher._initialize_handlers")
    def test_emit_event_creates_and_dispatches(
        self, mock_init_handlers, db_session
    ):
        from app.services.events.dispatcher import emit_event

        mock_db = MagicMock()
        sub_id = uuid.uuid4()
        event = emit_event(
            mock_db,
            EventType.subscriber_created,
            {"first_name": "Jane"},
            actor="system",
            subscriber_id=sub_id,
        )

        assert event.event_type == EventType.subscriber_created
        assert event.payload == {"first_name": "Jane"}
        assert event.actor == "system"
        assert event.subscriber_id == sub_id

    @patch("app.services.events.dispatcher._dispatcher", None)
    @patch("app.services.events.dispatcher._initialize_handlers")
    def test_emit_event_accepts_string_uuids(
        self, mock_init_handlers, db_session
    ):
        from app.services.events.dispatcher import emit_event

        mock_db = MagicMock()
        sub_id = uuid.uuid4()
        event = emit_event(
            mock_db,
            EventType.subscriber_created,
            {},
            subscriber_id=str(sub_id),
        )
        assert event.subscriber_id == sub_id
