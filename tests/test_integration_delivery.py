from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import httpx

from app.models.integration_platform import (
    IntegrationDelivery,
    IntegrationEventSubscription,
)
from app.services.events.types import Event, EventType
from app.services.integrations import installations
from app.services.integrations.connectors.http_webhook import HttpWebhookRunner
from app.services.integrations.delivery import (
    create_event_subscription,
    create_platform_deliveries_for_event,
    execute_delivery,
    replay_delivery,
)
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)


def _enabled_http_binding(db_session, client: httpx.Client):
    installation = installations.create_draft(
        db_session,
        connector_key="webhook.http",
        name=f"HTTP delivery {uuid4()}",
        actor="test-operator",
    )
    installations.create_config_revision(
        db_session,
        installation_id=installation.id,
        config={
            "url": "https://hooks.example.test/events",
            "method": "POST",
            "timeout_seconds": 5,
            "max_attempts": 3,
        },
        secret_refs={},
        actor="test-operator",
    )
    binding = installations.bind_capability(
        db_session,
        installation_id=installation.id,
        capability_id="events.deliver.v1",
        policy={"approved_egress_hosts": ["hooks.example.test"]},
        actor="test-operator",
    )
    assert installations.validate_static(
        db_session, installation_id=installation.id
    ).valid
    context = build_execution_context(
        db_session,
        capability_binding_id=binding.id,
        allow_disabled=True,
        runner_override=HttpWebhookRunner(client),
    )
    connection = validate_connection(context)
    assert connection.valid
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=connection,
        actor="test-operator",
    )
    return binding


def test_enabled_subscription_creates_one_idempotent_delivery(db_session) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(204, request=request)
        )
    )
    binding = _enabled_http_binding(db_session, client)
    subscription = create_event_subscription(
        db_session,
        capability_binding_id=binding.id,
        event_type="invoice.paid",
        actor="test-operator",
    )
    event = Event(
        event_type=EventType.invoice_paid,
        payload={"invoice_id": "invoice-1", "amount": "5000.00"},
    )

    first = create_platform_deliveries_for_event(
        db_session,
        event=event,
        event_type="invoice.paid",
    )
    replay = create_platform_deliveries_for_event(
        db_session,
        event=event,
        event_type="invoice.paid",
    )

    assert len(first) == len(replay) == 1
    assert first[0].id == replay[0].id
    assert first[0].state == "pending"
    assert first[0].capability_binding_id == binding.id
    assert subscription.state == "enabled"
    assert db_session.query(IntegrationEventSubscription).count() == 1
    assert db_session.query(IntegrationDelivery).count() == 1


def test_delivery_sends_only_through_typed_runner(db_session) -> None:
    requests: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    client = httpx.Client(transport=httpx.MockTransport(responder))
    binding = _enabled_http_binding(db_session, client)
    create_event_subscription(
        db_session,
        capability_binding_id=binding.id,
        event_type="invoice.paid",
    )
    event = Event(
        event_type=EventType.invoice_paid,
        payload={"invoice_id": "invoice-platform"},
    )
    delivery = create_platform_deliveries_for_event(
        db_session,
        event=event,
        event_type="invoice.paid",
    )[0]

    delivered = execute_delivery(
        db_session,
        delivery_id=delivery.id,
        runner_override=HttpWebhookRunner(client),
    )

    assert delivered.state == "delivered"
    assert delivered.attempt_count == 1
    assert [request.method for request in requests] == ["HEAD", "POST"]
    post = requests[-1]
    assert post.headers["Idempotency-Key"]
    assert post.headers["X-Dotmac-Event"] == "invoice.paid"
    assert b"invoice-platform" in post.content


def test_disabled_binding_fails_closed_and_is_not_selected(db_session) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(204, request=request)
        )
    )
    binding = _enabled_http_binding(db_session, client)
    create_event_subscription(
        db_session,
        capability_binding_id=binding.id,
        event_type="invoice.paid",
    )
    installations.disable_installation(
        db_session,
        installation_id=binding.installation_id,
        reason="test",
    )

    deliveries = create_platform_deliveries_for_event(
        db_session,
        event=Event(
            event_type=EventType.invoice_paid,
            payload={"invoice_id": "disabled"},
        ),
        event_type="invoice.paid",
    )

    assert deliveries == []


def test_ambiguous_delivery_requires_reconciliation_before_replay(db_session) -> None:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(204, request=request)
        raise httpx.ReadTimeout("ambiguous", request=request)

    client = httpx.Client(transport=httpx.MockTransport(responder))
    binding = _enabled_http_binding(db_session, client)
    create_event_subscription(
        db_session,
        capability_binding_id=binding.id,
        event_type="invoice.paid",
    )
    delivery = create_platform_deliveries_for_event(
        db_session,
        event=Event(
            event_type=EventType.invoice_paid,
            payload={"invoice_id": "ambiguous"},
        ),
        event_type="invoice.paid",
    )[0]

    execute_delivery(
        db_session,
        delivery_id=delivery.id,
        runner_override=HttpWebhookRunner(client),
    )

    assert delivery.state == "reconciliation_required"
    replay_delivery(db_session, delivery_id=delivery.id)
    assert delivery.state == "pending"


def test_delivery_migration_is_linear_and_contains_no_compatibility_columns() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/378_integration_delivery.py"
    )
    spec = importlib.util.spec_from_file_location("migration_374", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "378_integration_delivery"
    assert module.down_revision == "377_integration_capability_sync"
    source = path.read_text(encoding="utf-8")
    assert "integration_event_subscriptions" in source
    assert "integration_deliveries" in source
    assert "legacy_webhook" not in source
    assert "legacy_hook" not in source
    assert "send_suppressed" not in source
