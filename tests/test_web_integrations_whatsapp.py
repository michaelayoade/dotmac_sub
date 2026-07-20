from __future__ import annotations

import json

import pytest

from app.models.integration_platform import IntegrationInstallation
from app.models.notification import NotificationChannel, NotificationTemplate
from app.services import web_notifications
from app.services.integrations import installations, whatsapp_capability
from app.services.integrations.connectors import whatsapp_runtime
from app.services.integrations.runtime import ValidationResult
from app.services.web_integrations_whatsapp import (
    build_config_state,
    run_test_send,
    save_config,
)


def _save(db_session):
    return save_config(
        db_session,
        provider="meta_cloud_api",
        phone_number="445744508632976",
        waba_id="waba-1",
        webhook_url="https://sub.example.test/api/v1/webhooks/whatsapp/meta",
        graph_version="v21.0",
        api_key="bao://secret/integrations/whatsapp#token",
        api_secret="bao://secret/integrations/whatsapp#app_secret",
        webhook_verify_token="bao://secret/integrations/whatsapp#verify_token",
        message_templates_json='[{"name":"invoice_due","language":"en"}]',
    )


def test_save_config_creates_versioned_installation_with_secret_refs_only(db_session):
    installation = _save(db_session)
    state = build_config_state(db_session)

    assert db_session.query(IntegrationInstallation).one().id == installation.id
    revision = installation.current_config_revision
    assert revision.config_json["provider"] == "meta_cloud_api"
    assert revision.config_json["templates"][0]["name"] == "invoice_due"
    assert revision.secret_refs == {
        "service_credentials": "bao://secret/integrations/whatsapp#token",
        "webhook_signing_secret": ("bao://secret/integrations/whatsapp#app_secret"),
        "webhook_verify_token": ("bao://secret/integrations/whatsapp#verify_token"),
    }
    assert state["form"]["api_key"] == ""
    assert state["form"]["api_key_masked"].startswith("bao://")
    assert "invoice_due" in state["form"]["message_templates_json"]
    assert installation.state == "disabled"


def test_save_config_rejects_plaintext_credentials(db_session):
    with pytest.raises(ValueError, match="secret reference"):
        save_config(
            db_session,
            provider="meta_cloud_api",
            phone_number="phone-1",
            webhook_url="",
            api_key="plaintext-token",
            api_secret="",
            message_templates_json="[]",
        )
    assert db_session.query(IntegrationInstallation).count() == 1
    assert (
        db_session.query(IntegrationInstallation).one().current_config_revision is None
    )


def test_run_test_send_uses_enabled_capability_preview(db_session, monkeypatch):
    installation = _save(db_session)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    monkeypatch.setattr(
        whatsapp_capability,
        "send_template_message",
        lambda *args, **kwargs: {
            "ok": True,
            "provider": "meta_cloud_api",
            "sent": False,
            "payload": {"template": {"name": kwargs["template_name"]}},
        },
    )

    result = run_test_send(
        db_session,
        recipient="+2348111111111",
        template_name="invoice_reminder",
        variables_json='{"1":"Alice","2":"12000"}',
    )

    assert result["sent"] is False
    assert result["payload"]["template"]["name"] == "invoice_reminder"


def test_whatsapp_notification_template_test_uses_typed_capability(
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
    db_session.flush()
    captured = {}

    def fake_send_template_message(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "provider": "meta_cloud_api"}

    monkeypatch.setattr(
        whatsapp_capability,
        "send_template_message",
        fake_send_template_message,
    )
    message = web_notifications.send_template_test(
        db_session,
        template_id=template.id,
        test_recipient="2348169895859",
        test_variables_json="{}",
    )

    assert message == "Test WhatsApp message sent to 2348169895859"
    assert captured["template_name"] == "good_day"
    assert captured["dry_run"] is False


def test_runtime_normalizes_meta_webhook_without_settings():
    meta = whatsapp_runtime.normalize_inbound_webhook(
        provider="meta_cloud_api",
        payload={
            "message": {
                "from": "08012345678",
                "text": "Hello",
                "id": "wamid-1",
            }
        },
    )

    assert meta["normalized_from"] == "+2348012345678"
    assert meta["external_id"] == "wamid-1"


def test_save_config_rejects_invalid_templates_json(db_session):
    with pytest.raises(ValueError, match="JSON array"):
        save_config(
            db_session,
            provider="meta_cloud_api",
            phone_number="phone-1",
            webhook_url="",
            api_key="bao://secret/integrations/whatsapp#token",
            api_secret="",
            message_templates_json=json.dumps({"invalid": True}),
        )
