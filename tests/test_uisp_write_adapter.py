from __future__ import annotations

import copy

import pytest

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import CPEDevice, DeviceType, VendorModelCapability
from app.models.uisp_control import UispIntentTargetType
from app.services.uisp import UispUnsupportedOperationError
from app.services.uisp_control_plane import redact_config, stage_intent
from app.services.uisp_write_adapter import (
    UispConfigurationWriteAdapter,
    UispPostWriteReadbackError,
    UispWriteUnsupported,
)


class FakeClient:
    def __init__(self, initial, readbacks=None, put_error=None):
        self.initial = copy.deepcopy(initial)
        self.readbacks = [copy.deepcopy(item) for item in (readbacks or [])]
        self.put_error = put_error
        self.put_payloads = []
        self.get_calls = 0

    def get_device_configuration(self, device_id, *, transport):
        assert transport == "onu"
        self.get_calls += 1
        if self.get_calls == 1:
            return copy.deepcopy(self.initial)
        if self.readbacks:
            return copy.deepcopy(self.readbacks.pop(0))
        return copy.deepcopy(self.initial)

    def put_device_configuration(self, device_id, configuration, *, transport):
        assert transport == "onu"
        if self.put_error:
            raise self.put_error
        self.put_payloads.append(copy.deepcopy(configuration))
        return {"taskId": "uisp-task-1"}


def _intent(db_session, subscriber, catalog_offer, desired):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    cpe = CPEDevice(
        subscriber_id=subscriber.id,
        subscription=subscription,
        device_type=DeviceType.wireless_radio,
        vendor="ubiquiti",
        model="airCube-ISP",
        uisp_device_id="uisp-device-1",
    )
    capability = VendorModelCapability(
        vendor="ubiquiti",
        model="airCube-ISP",
        supported_features={
            "uisp": {
                "configuration_write": True,
                "transport": "onu",
                "fields": {
                    "name": "/system/name",
                    "wifi.ssid": "/wireless/ssid",
                    "wifi.password_ref": "/wireless/key",
                    "remote_access.enabled": "/services/ssh/enabled",
                },
            }
        },
    )
    db_session.add_all([subscription, cpe, capability])
    db_session.flush()
    return stage_intent(
        db_session,
        target_type=UispIntentTargetType.cpe,
        target_id=cpe.id,
        desired_state=desired,
    )


def _config(*, name="old", ssid="old-ssid", wifi_secret=None, ssh=False):
    if wifi_secret is None:
        wifi_secret = "old-password"
    return {
        "system": {"name": name, "untouched": "preserve"},
        "wireless": {"ssid": ssid, "key": wifi_secret, "channel": 6},
        "services": {"ssh": {"enabled": ssh}},
    }


def test_write_preserves_unknown_fields_and_requires_matching_readback(
    db_session, subscriber, catalog_offer
):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {
            "name": "customer-radio",
            "wifi": {"ssid": "Customer", "password_ref": "plain:Secret123"},
            "remote_access": {"enabled": True},
        },
    )
    expected = _config(
        name="customer-radio", ssid="Customer", wifi_secret="Secret123", ssh=True
    )
    client = FakeClient(_config(), readbacks=[expected])

    result = UispConfigurationWriteAdapter(client, readback_delay_seconds=0).apply(
        db_session, intent
    )

    assert result.verified is True
    assert result.write_accepted is True
    assert result.attempts == 1
    assert client.put_payloads == [expected]
    assert client.put_payloads[0]["system"]["untouched"] == "preserve"
    assert client.put_payloads[0]["wireless"]["channel"] == 6
    assert result.observed_config["wifi.password_ref"] == "[verified]"
    assert "Secret123" not in str(result.to_dict())


def test_write_is_not_success_when_readback_does_not_converge(
    db_session, subscriber, catalog_offer
):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"wifi": {"ssid": "Customer"}},
    )
    client = FakeClient(_config(), readbacks=[_config(), _config()])

    result = UispConfigurationWriteAdapter(
        client, readback_attempts=2, readback_delay_seconds=0
    ).apply(db_session, intent)

    assert result.verified is False
    assert result.outcome == "drifted"
    assert result.write_accepted is True
    assert result.attempts == 2
    assert result.drift["wifi.ssid"]["observed"] == "old-ssid"


def test_readback_only_never_invokes_put(db_session, subscriber, catalog_offer):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"wifi": {"ssid": "Customer"}},
    )
    client = FakeClient(_config(ssid="Customer"))

    result = UispConfigurationWriteAdapter(
        client, readback_attempts=1, readback_delay_seconds=0
    ).readback(db_session, intent)

    assert result.verified is True
    assert result.write_accepted is False
    assert client.put_payloads == []


def test_redaction_covers_common_pre_shared_key_names():
    redacted = redact_config(
        {"psk": "secret-1", "pre_shared_key": "secret-2", "presharedKey": "secret-3"}
    )

    assert redacted == {
        "psk": "[redacted]",
        "pre_shared_key": "[redacted]",
        "presharedKey": "[redacted]",
    }


def test_uisp_501_becomes_model_unsupported(db_session, subscriber, catalog_offer):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"wifi": {"ssid": "Customer"}},
    )
    client = FakeClient(
        _config(),
        put_error=UispUnsupportedOperationError(
            "setServices is not implemented", status_code=501
        ),
    )

    with pytest.raises(UispWriteUnsupported, match="does not implement"):
        UispConfigurationWriteAdapter(client).apply(db_session, intent)


def test_missing_json_path_refuses_write_without_mutating_device(
    db_session, subscriber, catalog_offer
):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"wifi": {"ssid": "Customer"}},
    )
    client = FakeClient({"system": {"name": "radio"}})

    with pytest.raises(UispWriteUnsupported, match="parent does not exist"):
        UispConfigurationWriteAdapter(client).apply(db_session, intent)

    assert client.put_payloads == []


def test_unmapped_desired_field_refuses_entire_write(
    db_session, subscriber, catalog_offer
):
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"firmware_version": "9.0.0", "wifi": {"ssid": "Customer"}},
    )
    client = FakeClient(_config())

    with pytest.raises(UispWriteUnsupported, match="firmware_version"):
        UispConfigurationWriteAdapter(client).apply(db_session, intent)

    assert client.put_payloads == []


def test_non_client_readback_failure_after_an_accepted_write_is_recoverable(
    db_session, subscriber, catalog_offer
):
    """A write that has already hit the device must never be recorded as a plain
    failure, whatever the readback blows up with.

    The post-write guard used to catch only UispClientError. An unexpected device
    payload raising KeyError/TypeError inside the comparison escaped untranslated,
    reached the task's generic handler with ``result`` still None, and was recorded
    as `failed` -- a status the reconciler does not sweep. The device would then sit
    silently diverged from its desired_state with nothing left to notice.
    """
    intent = _intent(
        db_session,
        subscriber,
        catalog_offer,
        {"name": "customer-radio"},
    )

    class ExplodingReadbackClient(FakeClient):
        """Serves the pre-write read, then explodes on the readback.

        apply() reads the device twice: once to compute the proposed config,
        once to read back after writing. Only the second must fail, or the write
        never happens and the guard under test is never reached.
        """

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.reads = 0

        def get_device_configuration(self, *args, **kwargs):
            self.reads += 1
            if self.reads == 1:
                return super().get_device_configuration(*args, **kwargs)
            # Not a UispClientError -- the kind of thing a malformed device
            # response actually raises.
            raise KeyError("unexpected payload shape")

    client = ExplodingReadbackClient(_config())

    with pytest.raises(UispPostWriteReadbackError):
        UispConfigurationWriteAdapter(client, readback_delay_seconds=0).apply(
            db_session, intent
        )

    # The write was still issued -- that is precisely why this must be recoverable
    # rather than terminal.
    assert client.put_payloads
