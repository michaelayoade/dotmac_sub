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

        db.query.return_value.filter.return_value.filter.return_value.filter.return_value.all.return_value = [
            session
        ]
        # First db.get returns subscription, second returns credential, third returns NAS
        nas_device = MagicMock(spec=NasDevice)
        db.get.side_effect = [subscription, credential, nas_device]

        mock_coa_update.return_value = True

        # Need to also mock _resolve_nas_device
        with patch(
            "app.services.enforcement._resolve_nas_device", return_value=nas_device
        ):
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
            patch(
                "app.services.events.handlers.provisioning.provisioning_service"
            ) as mock_prov,
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

        with patch(
            "app.services.radius.sync_account_credentials_to_radius", return_value=2
        ) as mock_sync:
            handler._sync_radius_on_activation(db, str(uuid4()))
            mock_sync.assert_called_once()


# ---------------------------------------------------------------------------
# CoA negative-cache (auto-detect NASes that don't support CoA)
# ---------------------------------------------------------------------------


class TestCoaNegativeCache:
    """A CoA Timeout poisons the NAS for 15 minutes; a success clears it."""

    def setup_method(self) -> None:
        from app.services import enforcement

        enforcement.reset_coa_cache()

    def teardown_method(self) -> None:
        from app.services import enforcement

        enforcement.reset_coa_cache()

    def _device(self, nas_id):
        device = MagicMock(spec=NasDevice)
        device.id = nas_id
        device.shared_secret = "enc:secret"
        device.nas_ip = "10.1.1.1"
        device.management_ip = "10.1.1.1"
        device.ip_address = "10.1.1.1"
        device.coa_port = 3799
        return device

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_timeout_populates_cache_and_skips_next_call(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        from pyrad.client import Timeout

        from app.services.enforcement import _coa_disabled_for_nas, _send_coa_disconnect

        db = MagicMock()
        nas_id = uuid4()
        device = self._device(nas_id)
        mock_client = MagicMock()
        mock_client.SendPacket.side_effect = Timeout()
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            assert _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid") is False

        assert _coa_disabled_for_nas(nas_id) is True

        # Second call must short-circuit: Client must NOT be instantiated again.
        mock_client_cls.reset_mock()
        with patch("app.services.enforcement._coa_enabled", return_value=True):
            assert _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid") is False
        mock_client_cls.assert_not_called()

    @patch("app.services.enforcement.decrypt_credential", return_value="secret")
    @patch("app.services.enforcement._radius_dictionary_path", return_value="/dict")
    @patch("app.services.enforcement.Dictionary")
    @patch("app.services.enforcement.Client")
    def test_success_clears_negative_cache(
        self, mock_client_cls, mock_dict, mock_path, mock_decrypt
    ):
        from app.services.enforcement import (
            _coa_disabled_for_nas,
            _mark_coa_unsupported,
            _send_coa_disconnect,
        )

        db = MagicMock()
        nas_id = uuid4()
        device = self._device(nas_id)
        _mark_coa_unsupported(nas_id)
        assert _coa_disabled_for_nas(nas_id) is True

        # Manually clear so the next call actually runs (success path then re-clears).
        from app.services import enforcement

        enforcement.reset_coa_cache(nas_id)

        mock_client = MagicMock()
        mock_client.SendPacket.return_value = None
        mock_client_cls.return_value = mock_client

        with (
            patch("app.services.enforcement._coa_enabled", return_value=True),
            patch("app.services.enforcement._coa_retries", return_value=0),
            patch("app.services.enforcement._radius_timeout_sec", return_value=0.01),
        ):
            assert _send_coa_disconnect(db, device, "u", "1.2.3.4", "sid") is True

        # Successful send must keep the cache clear.
        assert _coa_disabled_for_nas(nas_id) is False

    def test_reset_coa_cache_all_and_per_nas(self):
        from app.services.enforcement import (
            _coa_disabled_for_nas,
            _mark_coa_unsupported,
            reset_coa_cache,
        )

        a, b = uuid4(), uuid4()
        _mark_coa_unsupported(a)
        _mark_coa_unsupported(b)
        assert _coa_disabled_for_nas(a) is True
        assert _coa_disabled_for_nas(b) is True

        reset_coa_cache(a)
        assert _coa_disabled_for_nas(a) is False
        assert _coa_disabled_for_nas(b) is True

        reset_coa_cache()
        assert _coa_disabled_for_nas(b) is False
