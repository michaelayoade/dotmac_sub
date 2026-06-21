from __future__ import annotations

import json

import pytest

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel, NotificationTemplate
from app.services import domain_settings as domain_settings_service
from app.services import web_notifications
from app.services.credential_crypto import is_encrypted
from app.services.integrations.connectors import whatsapp as whatsapp_connector
from app.services.secrets import is_secret_ref
from app.services.settings_spec import resolve_value
from app.services.web_integrations_whatsapp import (
    build_config_state,
    run_test_send,
    save_config,
)


def test_save_config_persists_and_masks_credentials(db_session):
    save_config(
        db_session,
        provider="twilio",
        phone_number="+2348000000000",
        webhook_url="https://example.com/webhook/whatsapp",
        api_key="key-123456",
        api_secret="secret-654321",
        message_templates_json='[{"name":"invoice_due","body":"Hi {name}"}]',
    )

    state = build_config_state(db_session)

    assert state["form"]["provider"] == "twilio"
    assert state["form"]["phone_number"] == "+2348000000000"
    assert state["form"]["webhook_url"] == "https://example.com/webhook/whatsapp"
    assert state["form"]["api_key_masked"].endswith("3456")
    assert state["form"]["api_secret_masked"].endswith("4321")
    assert "invoice_due" in state["form"]["message_templates_json"]

    stored_key = domain_settings_service.comms_settings.get_by_key(
        db_session, "whatsapp_api_key"
    )
    stored_secret = domain_settings_service.comms_settings.get_by_key(
        db_session, "whatsapp_api_secret"
    )
    assert stored_key.value_text
    assert stored_secret.value_text
    # The credential must never be persisted as bare plaintext. Depending on the
    # environment it is stored either encryption-at-rest (``enc:``/``plain:``) or
    # behind a secret reference (e.g. ``bao://`` when OpenBao is configured, with
    # the encrypted value living in the secret store). Assert it is one of those
    # protected forms — not the raw secret — and that it still round-trips.
    for stored, original in (
        (stored_key.value_text, "key-123456"),
        (stored_secret.value_text, "secret-654321"),
    ):
        stored = str(stored)
        assert original not in stored
        assert is_encrypted(stored) or is_secret_ref(stored)

    config = whatsapp_connector.load_whatsapp_config(db_session)
    assert config["api_key"] == "key-123456"
    assert config["api_secret"] == "secret-654321"


def test_run_test_send_uses_current_configuration(db_session):
    save_config(
        db_session,
        provider="meta_cloud_api",
        phone_number="+2348000000000",
        webhook_url="https://example.com/webhook/whatsapp",
        api_key="meta-key-1",
        api_secret="meta-secret-1",
        message_templates_json="[]",
    )

    result = run_test_send(
        db_session,
        recipient="+2348111111111",
        template_name="invoice_reminder",
        variables_json='{"name":"Alice","amount":"12000"}',
    )

    assert result["ok"] is True
    assert result["provider"] == "meta_cloud_api"
    assert result["sent"] is False
    assert result["payload"]["template"]["name"] == "invoice_reminder"


def test_meta_send_uses_phone_number_id_endpoint(db_session, monkeypatch):
    save_config(
        db_session,
        provider="meta_cloud_api",
        phone_number="445744508632976",
        webhook_url="",
        api_key="meta-token",
        api_secret="",
        message_templates_json="[]",
    )

    captured = {}

    class Response:
        status_code = 200
        text = "{}"

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(whatsapp_connector.httpx, "post", fake_post)

    result = whatsapp_connector.send_template_message(
        db_session,
        recipient="2348169895859",
        template_name="good_day",
        dry_run=False,
    )

    assert result["ok"] is True
    assert captured["url"].endswith("/445744508632976/messages")
    assert captured["headers"]["Authorization"] == "Bearer meta-token"
    assert captured["json"]["template"]["name"] == "good_day"


def test_whatsapp_notification_template_test_uses_template_code(
    db_session, monkeypatch
):
    template = NotificationTemplate(
        name="Good Day",
        code="good_day",
        channel=NotificationChannel.whatsapp,
        body="good_day",
        is_active=True,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)

    captured = {}

    def fake_send_template_message(
        db, *, recipient, template_name, language=None, variables=None, dry_run=True
    ):
        captured["recipient"] = recipient
        captured["template_name"] = template_name
        captured["language"] = language
        captured["dry_run"] = dry_run
        return {"ok": True, "provider": "meta_cloud_api"}

    monkeypatch.setattr(
        whatsapp_connector, "send_template_message", fake_send_template_message
    )

    message = web_notifications.send_template_test(
        db_session,
        template_id=template.id,
        test_recipient="2348169895859",
        test_variables_json="{}",
    )

    assert message == "Test WhatsApp message sent to 2348169895859"
    assert captured == {
        "recipient": "2348169895859",
        "template_name": "good_day",
        "language": None,
        "dry_run": False,
    }


def test_save_config_rejects_invalid_templates_json(db_session):
    with pytest.raises(ValueError):
        save_config(
            db_session,
            provider="meta_cloud_api",
            phone_number="+2348000000000",
            webhook_url="",
            api_key="key",
            api_secret="secret",
            message_templates_json='{"invalid":true}',
        )


def test_whatsapp_connector_normalize_webhook_shapes():
    twilio = whatsapp_connector.normalize_inbound_webhook(
        provider="twilio",
        payload={
            "From": "whatsapp:08012345678",
            "Body": "Hello",
            "MessageSid": "sid-1",
        },
    )
    assert twilio["from"] == "whatsapp:08012345678"
    assert twilio["normalized_from"] == "+2348012345678"
    assert twilio["external_id"] == "sid-1"

    messagebird = whatsapp_connector.normalize_inbound_webhook(
        provider="messagebird",
        payload={"from": "08081112222", "text": "Yo", "id": "msg-2"},
    )
    assert messagebird["text"] == "Yo"
    assert messagebird["normalized_from"] == "+2348081112222"


def test_settings_spec_keys_resolve_for_whatsapp(db_session):
    save_config(
        db_session,
        provider="messagebird",
        phone_number="+2348000000000",
        webhook_url="https://example.com/wa",
        api_key="mb-key",
        api_secret="mb-secret",
        message_templates_json=json.dumps([{"name": "n1"}]),
    )
    assert (
        resolve_value(db_session, SettingDomain.comms, "whatsapp_provider")
        == "messagebird"
    )
    assert (
        resolve_value(db_session, SettingDomain.comms, "whatsapp_phone_number")
        == "+2348000000000"
    )
