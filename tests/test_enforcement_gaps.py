"""Tests for enforcement gap implementations.

Covers:
- CoA-Update for mid-session speed changes
- Hotspot session disconnect
- Subscription cancellation cleanup
- EnforcementHandler event routing
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.catalog import (
    AccessCredential,
    NasDevice,
    RadiusProfile,
    Subscription,
)
from app.services.events.types import Event, EventType

# ---------------------------------------------------------------------------
# _build_mikrotik_rate_limit
# ---------------------------------------------------------------------------


class TestBuildMikrotikRateLimit:
    def test_returns_explicit_rate_limit(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = "50M/25M"
        profile.download_speed = 50000
        profile.upload_speed = 25000
        assert _build_mikrotik_rate_limit(profile) == "50M/25M"

    def test_builds_from_speeds(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = 50000
        profile.upload_speed = 25000
        profile.burst_download = None
        profile.burst_upload = None
        profile.burst_threshold = None
        profile.burst_time = None
        result = _build_mikrotik_rate_limit(profile)
        assert result == "50000k/25000k"

    def test_builds_with_burst(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = 50000
        profile.upload_speed = 25000
        profile.burst_download = 75000
        profile.burst_upload = 37500
        profile.burst_threshold = 40000
        profile.burst_time = 15
        result = _build_mikrotik_rate_limit(profile)
        assert "50000k/25000k" in result
        assert "75000k/37500k" in result
        assert "15/15" in result

    def test_returns_none_no_speeds(self):
        from app.services.enforcement import _build_mikrotik_rate_limit

        profile = MagicMock(spec=RadiusProfile)
        profile.mikrotik_rate_limit = None
        profile.download_speed = None
        profile.upload_speed = None
        assert _build_mikrotik_rate_limit(profile) is None


# ---------------------------------------------------------------------------
# update_subscription_sessions (CoA-Update)
# ---------------------------------------------------------------------------


class TestUpdateSubscriptionSessions:
    @patch("app.services.enforcement._send_coa_update")
    @patch("app.services.enforcement._resolve_effective_profile")
    def test_sends_coa_update(self, mock_profile, mock_coa_update):
        from app.services.enforcement import update_subscription_sessions

        db = MagicMock()
        sub_id = uuid4()
        subscription = MagicMock(spec=Subscription)
        subscription.id = sub_id
        subscription.ipv4_address = "10.0.0.1"
        db.get.return_value = subscription

        profile = MagicMock(spec=RadiusProfile)
        mock_profile.return_value = profile

        session = MagicMock()
        session.access_credential_id = uuid4()
        session.session_id = "sess-123"
        credential = MagicMock(spec=AccessCredential)
        credential.username = "user1"

        db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [session]
        # First db.get returns subscription, second returns credential, third returns NAS
        nas_device = MagicMock(spec=NasDevice)
        db.get.side_effect = [subscription, credential, nas_device]

        mock_coa_update.return_value = True

        # Need to also mock _resolve_nas_device
        with patch("app.services.enforcement._resolve_nas_device", return_value=nas_device):
            count = update_subscription_sessions(db, str(sub_id))

        assert count == 1
        mock_coa_update.assert_called_once()

    @patch("app.services.enforcement._resolve_effective_profile")
    def test_skips_if_no_profile(self, mock_profile):
        from app.services.enforcement import update_subscription_sessions

        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        db.get.return_value = subscription
        mock_profile.return_value = None

        count = update_subscription_sessions(db, str(uuid4()))
        assert count == 0


# ---------------------------------------------------------------------------
# EnforcementHandler event routing
# ---------------------------------------------------------------------------


class TestEnforcementHandlerRouting:
    def test_routes_upgraded_to_speed_change(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_upgraded,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_speed_change") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_downgraded_to_speed_change(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_downgraded,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_speed_change") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_canceled_to_cleanup(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_canceled,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_cancel") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)

    def test_routes_suspended_to_block(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_suspended,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_block") as mock:
            handler.handle(db, event)
            mock.assert_called_once()

    def test_routes_activated_to_restore(self):
        from app.services.events.handlers.enforcement import EnforcementHandler

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"subscription_id": str(uuid4())},
            subscription_id=uuid4(),
        )
        db = MagicMock()
        with patch.object(handler, "_handle_subscription_restore") as mock:
            handler.handle(db, event)
            mock.assert_called_once_with(db, event)


# ---------------------------------------------------------------------------
# ProvisioningHandler auto-provisioning
# ---------------------------------------------------------------------------


class TestProvisioningHandlerAutoProvisioning:
    def test_activation_syncs_radius(self):
        from app.services.events.handlers.provisioning import ProvisioningHandler

        handler = ProvisioningHandler()
        sub_id = uuid4()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"subscription_id": str(sub_id)},
            subscription_id=sub_id,
        )
        db = MagicMock()
        with (
            patch.object(handler, "_sync_radius_on_activation") as mock_sync,
            patch.object(handler, "_push_nas_provisioning") as mock_push,
            patch("app.services.events.handlers.provisioning.provisioning_service") as mock_prov,
        ):
            handler._handle_subscription_activated(db, event)
            mock_sync.assert_called_once_with(db, str(sub_id))
            mock_push.assert_called_once_with(db, str(sub_id))

    @patch("app.services.events.handlers.provisioning.coerce_uuid")
    def test_sync_radius_calls_service(self, mock_coerce):
        from app.services.events.handlers.provisioning import ProvisioningHandler

        handler = ProvisioningHandler()
        db = MagicMock()
        subscription = MagicMock(spec=Subscription)
        subscription.subscriber_id = uuid4()
        db.get.return_value = subscription
        mock_coerce.return_value = subscription.id

        with patch("app.services.radius.sync_account_credentials_to_radius", return_value=2) as mock_sync:
            handler._sync_radius_on_activation(db, str(uuid4()))
            mock_sync.assert_called_once()
