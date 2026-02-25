from __future__ import annotations

import json

import pytest

from app.models.domain_settings import SettingDomain
from app.services import domain_settings as domain_settings_service
from app.services.integrations.connectors import whatsapp as whatsapp_connector
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

    stored_key = domain_settings_service.comms_settings.get_by_key(db_session, "whatsapp_api_key")
    stored_secret = domain_settings_service.comms_settings.get_by_key(db_session, "whatsapp_api_secret")
    assert stored_key.value_text
    assert stored_secret.value_text
    assert str(stored_key.value_text).startswith(("plain:", "enc:"))
    assert str(stored_secret.value_text).startswith(("plain:", "enc:"))


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
        payload={"From": "whatsapp:+1", "Body": "Hello", "MessageSid": "sid-1"},
    )
    assert twilio["from"] == "whatsapp:+1"
    assert twilio["external_id"] == "sid-1"

    messagebird = whatsapp_connector.normalize_inbound_webhook(
        provider="messagebird",
        payload={"from": "+2", "text": "Yo", "id": "msg-2"},
    )
    assert messagebird["text"] == "Yo"


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
    assert resolve_value(db_session, SettingDomain.comms, "whatsapp_provider") == "messagebird"
    assert resolve_value(db_session, SettingDomain.comms, "whatsapp_phone_number") == "+2348000000000"
