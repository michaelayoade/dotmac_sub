"""SMS must fail closed when unconfigured.

Production evidence (2026-07-23): 0 SMS ever delivered, against 4,053
`expired_in_queue` and 716 `send_failed` rows. `sms_enabled` defaulted to
"true" and the provider defaulted to "webhook" with no webhook URL, so a
deployment that had never configured SMS still presented the channel as live
and queued sends into nothing.
"""

import pytest

from app.services import sms as sms_service
from app.services import web_notifications


@pytest.fixture
def unconfigured(monkeypatch):
    """No SMS settings and no SMS env vars — a fresh deployment."""

    def _get(_db, _key, _env, default=None):
        return default

    monkeypatch.setattr(sms_service, "_get_setting", _get)
    monkeypatch.setattr(web_notifications.sms_service, "_get_setting", _get)


def test_unconfigured_sms_does_not_send(unconfigured):
    """The default must be off, not 'on and pointed at nothing'."""
    assert sms_service.send_sms(None, "+2348000000000", "hi", track=False) is False


def test_unconfigured_sms_reports_not_ready(unconfigured):
    ready, message = web_notifications._sms_channel_ready(None)
    assert ready is False
    assert "disabled" in message.lower()


def test_readiness_probe_agrees_with_the_send_path(unconfigured):
    """A channel the operator is told is ready must actually attempt a send.

    These two drifting apart is how a dead channel stays invisible.
    """
    ready, _ = web_notifications._sms_channel_ready(None)
    sent = sms_service.send_sms(None, "+2348000000000", "hi", track=False)
    assert ready == sent


def test_enabled_without_a_provider_is_not_ready(monkeypatch):
    values = {"sms_enabled": "true"}

    def _get(_db, key, _env, default=None):
        return values.get(key, default)

    monkeypatch.setattr(web_notifications.sms_service, "_get_setting", _get)
    ready, message = web_notifications._sms_channel_ready(None)
    assert ready is False
    assert "provider" in message.lower()


def test_enabled_provider_still_needs_its_credentials(monkeypatch):
    values = {"sms_enabled": "true", "sms_provider": "africastalking"}

    def _get(_db, key, _env, default=None):
        return values.get(key, default)

    monkeypatch.setattr(web_notifications.sms_service, "_get_setting", _get)
    ready, message = web_notifications._sms_channel_ready(None)
    assert ready is False
    assert "api key" in message.lower()

    values["sms_api_key"] = "k"
    ready, _ = web_notifications._sms_channel_ready(None)
    assert ready is True


def test_explicit_enable_is_required_not_merely_non_false(monkeypatch):
    """ "" or a typo must not read as enabled."""
    for value in ("", "  ", "maybe", "0", "no"):
        monkeypatch.setattr(
            web_notifications.sms_service,
            "_get_setting",
            lambda _db, key, _env, default=None, v=value: (
                v if key == "sms_enabled" else default
            ),
        )
        ready, _ = web_notifications._sms_channel_ready(None)
        assert ready is False, f"{value!r} should not enable SMS"


# --- SMS retired via the channel disable mechanism --------------------------


def test_absent_sms_config_reads_as_disabled(monkeypatch):
    """Retirement is the default: with no sms_enabled row, SMS is disabled, so
    a spec that still defaults to SMS is cancelled cleanly at queue time rather
    than created and left to fail."""
    from app.models.notification import NotificationChannel
    from app.services import customer_notification_policy as policy

    monkeypatch.setattr(
        "app.services.sms._get_setting",
        lambda _db, _key, _env, default=None: default,
    )
    assert policy.channel_disabled_in_config(None, NotificationChannel.sms) is True


def test_a_future_plugin_re_enables_sms_by_flipping_the_flag(monkeypatch):
    """Nothing is deleted — enabling the channel brings it back."""
    from app.models.notification import NotificationChannel
    from app.services import customer_notification_policy as policy

    values = {"sms_enabled": "true"}
    monkeypatch.setattr(
        "app.services.sms._get_setting",
        lambda _db, key, _env, default=None: values.get(key, default),
    )
    assert policy.channel_disabled_in_config(None, NotificationChannel.sms) is False


def test_matrix_marks_a_disabled_channel_unavailable(monkeypatch):
    """A config-disabled channel (retired SMS) is marked unavailable; a merely
    not-ready-but-enabled channel is not."""
    from app.services import notification_channel_policy as channel_policy
    from app.services import web_notification_channels as view

    monkeypatch.setattr(
        view,
        "_channel_readiness",
        lambda db: {
            "email": (True, ""),
            "sms": (False, "SMS is disabled"),
            "whatsapp": (False, "not configured yet"),
        },
    )
    monkeypatch.setattr(
        view,
        "channel_disabled_in_config",
        lambda db, channel: channel.value == "sms",
    )
    monkeypatch.setattr(
        channel_policy,
        "get_channel_policy",
        lambda db: {"default": [], "categories": {}, "events": {}},
    )
    monkeypatch.setattr(channel_policy, "legacy_event_overrides", lambda db: {})

    channels = {
        c["id"]: c for c in view.channel_policy_context(None)["channel_policy_channels"]
    }
    assert channels["sms"]["disabled"] is True
    # whatsapp is not ready but NOT disabled -> stays selectable with a warning
    assert channels["whatsapp"]["disabled"] is False


def test_save_drops_a_hand_posted_disabled_channel(monkeypatch):
    """The checkbox is disabled in the UI; the writer defends the POST too.

    Only a config-disabled channel is dropped, never a merely not-ready one."""
    from app.services import notification_channel_policy as channel_policy
    from app.services import web_notification_channels as view

    monkeypatch.setattr(
        view,
        "channel_disabled_in_config",
        lambda db, channel: channel.value == "sms",
    )
    written = {}
    monkeypatch.setattr(
        channel_policy,
        "set_channel_policy",
        lambda db, **kw: written.update(kw) or {},
    )

    class _Form(dict):
        def getlist(self, key):
            v = self.get(key, [])
            return v if isinstance(v, list) else [v]

    view.save_channel_policy(None, _Form({view.DEFAULT_FIELD: ["email", "sms"]}))
    assert written["default"] == ["email"]
