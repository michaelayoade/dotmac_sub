from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.models.integration_platform import IntegrationInbox
from app.services.integrations import inbox, installations, whatsapp_capability
from app.services.integrations.connectors import whatsapp_runtime
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)
from app.services.integrations.whatsapp_capability import (
    WHATSAPP_RECEIVE_CAPABILITY,
    WHATSAPP_SEND_CAPABILITY,
)


def _envelope(*, action: str, params: dict) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test:whatsapp:1",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=WHATSAPP_SEND_CAPABILITY,
        connector_key="whatsapp",
        connector_version="1.0.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.event,
        idempotency_key="test-whatsapp-operation",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={"action": action, "params": params},
    )


def install_whatsapp(db_session, *, default: bool = True):
    installation = installations.create_draft(
        db_session,
        connector_key="whatsapp",
        name=f"WhatsApp {uuid4().hex}",
        environment="test",
        actor="test-operator",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "provider": "meta_cloud_api",
            "phone_number": "phone-1",
            "graph_version": "v21.0",
            "timeout_seconds": 10,
        },
        secret_refs={
            "service_credentials": "env://WHATSAPP_TEST_TOKEN",
            "webhook_signing_secret": "env://WHATSAPP_TEST_SIGNING_SECRET",
            "webhook_verify_token": "env://WHATSAPP_TEST_VERIFY_TOKEN",
        },
        actor="test-operator",
    )
    bindings = {}
    for capability_id in (WHATSAPP_SEND_CAPABILITY, WHATSAPP_RECEIVE_CAPABILITY):
        bindings[capability_id] = installations.bind_capability(
            db_session,
            installation_id=installation.id,
            capability_id=capability_id,
            scope={"channel": "whatsapp"},
            policy={"default": default},
            actor="test-operator",
        )
    installations.validate_static(db_session, installation_id=installation.id)
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
        actor="test-operator",
    )
    return installation, bindings


def test_whatsapp_runtime_preview_never_calls_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        whatsapp_runtime.httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("preview performed a provider send"),
    )
    result = whatsapp_runtime.WhatsAppRuntimeRunner().execute(
        _envelope(
            action="send_text",
            params={
                "recipient": "+2348000000001",
                "body": "Service restored",
                "preview": True,
            },
        ),
        config={"provider": "meta_cloud_api", "phone_number": "phone-1"},
        secret_material={},
    )

    assert result.status == OperationStatus.succeeded
    assert result.output["sent"] is False
    assert result.output["payload"]["text"] == {"body": "Service restored"}


def test_whatsapp_runtime_classifies_ambiguous_timeout(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("provider outcome unknown")

    monkeypatch.setattr(whatsapp_runtime.httpx, "post", timeout)
    result = whatsapp_runtime.WhatsAppRuntimeRunner().execute(
        _envelope(
            action="send_text",
            params={"recipient": "+2348000000001", "body": "Service restored"},
        ),
        config={"provider": "meta_cloud_api", "phone_number": "phone-1"},
        secret_material={"service_credentials": "runtime-only-test-token"},
    )

    assert result.status == OperationStatus.reconciliation_required
    assert result.error_code == "provider_outcome_ambiguous"


def test_whatsapp_facade_uses_only_enabled_typed_runtime(
    db_session, monkeypatch
) -> None:
    install_whatsapp(db_session)
    provider_calls: list[dict] = []

    def provider_send(url, *, json, headers, timeout):
        provider_calls.append({"url": url, "payload": json, "headers": headers})
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.platform-1"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(whatsapp_runtime.httpx, "post", provider_send)
    result = whatsapp_capability.send_text_message(
        db_session,
        recipient="+2348000000099",
        body="Platform path",
        correlation_id="notification:platform:1",
        secret_resolver=lambda _reference: "runtime-only-test-token",
    )

    assert len(provider_calls) == 1
    assert result["ok"] is True
    assert result["operation_status"] == "succeeded"
    assert result["provider_message_id"] == "wamid.platform-1"


def test_whatsapp_facade_fails_closed_without_or_with_ambiguous_binding(
    db_session,
) -> None:
    with pytest.raises(installations.InstallationError, match="no enabled binding"):
        whatsapp_capability.send_text_message(
            db_session,
            recipient="+2348000000099",
            body="No fallback",
            secret_resolver=lambda _reference: "token",
        )

    install_whatsapp(db_session, default=False)
    install_whatsapp(db_session, default=False)
    with pytest.raises(installations.InstallationError, match="exactly one"):
        whatsapp_capability.send_text_message(
            db_session,
            recipient="+2348000000099",
            body="Ambiguous",
            secret_resolver=lambda _reference: "token",
        )


def test_inbox_deduplicates_and_quarantines_identity_collision(db_session) -> None:
    _installation, bindings = install_whatsapp(db_session)
    receive_binding = bindings[WHATSAPP_RECEIVE_CAPABILITY]
    first, created = inbox.receive_verified(
        db_session,
        capability_binding_id=receive_binding.id,
        provider_event_id="meta:event-1",
        event_type="whatsapp.meta.webhook",
        payload={"entry": [{"id": "one"}]},
    )
    replay, replay_created = inbox.receive_verified(
        db_session,
        capability_binding_id=receive_binding.id,
        provider_event_id="meta:event-1",
        event_type="whatsapp.meta.webhook",
        payload={"entry": [{"id": "one"}]},
    )

    assert created is True
    assert replay_created is False
    assert replay.id == first.id
    assert db_session.query(IntegrationInbox).count() == 1

    with pytest.raises(inbox.InboxError, match="identity collision"):
        inbox.receive_verified(
            db_session,
            capability_binding_id=receive_binding.id,
            provider_event_id="meta:event-1",
            event_type="whatsapp.meta.webhook",
            payload={"entry": [{"id": "different"}]},
        )
    assert receive_binding.installation.state == "quarantined"
