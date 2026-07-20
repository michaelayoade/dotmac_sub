from __future__ import annotations

from app.models.integration_platform import (
    IntegrationDelivery,
    IntegrationEventSubscription,
)
from app.services import queue_adapter
from app.services import web_integrations_webhooks as webhooks
from app.services.integrations import installations
from app.services.integrations.runtime import ValidationResult


def test_webhook_admin_creates_typed_installation_and_subscriptions(db_session):
    endpoint = webhooks.create_webhook_endpoint(
        db_session,
        name="Billing events",
        url="https://hooks.example.test/events",
        signing_secret_ref="bao://secret/integrations/hooks#signing",
        authorization_ref="bao://secret/integrations/hooks#authorization",
        event_types=["invoice.created", "invoice.paid"],
        is_active=False,
        delivery_timeout_seconds=8,
        max_retries=4,
    )

    assert endpoint.connector_key == "webhook.http"
    assert endpoint.state == "disabled"
    assert endpoint.current_config_revision.config_json == {
        "url": "https://hooks.example.test/events",
        "method": "POST",
        "timeout_seconds": 8,
        "max_attempts": 4,
    }
    assert endpoint.current_config_revision.secret_refs == {
        "signing_secret": "bao://secret/integrations/hooks#signing",
        "authorization": "bao://secret/integrations/hooks#authorization",
    }
    assert {
        item.event_type for item in db_session.query(IntegrationEventSubscription).all()
    } == {"invoice.created", "invoice.paid"}

    edit = webhooks.build_webhook_edit_data(
        db_session,
        endpoint_id=str(endpoint.id),
    )
    assert edit["form"]["signing_secret_ref"] == ""
    assert edit["form"]["signing_secret_ref_configured"] is True


def test_webhook_admin_test_delivery_uses_canonical_worker(db_session, monkeypatch):
    endpoint = webhooks.create_webhook_endpoint(
        db_session,
        name="Operations events",
        url="https://hooks.example.test/operations",
        signing_secret_ref=None,
        authorization_ref=None,
        event_types=["network.alert"],
        is_active=False,
    )
    installations.enable_after_connection_validation(
        db_session,
        installation_id=endpoint.id,
        connection_result=ValidationResult(valid=True),
    )
    queued = {}

    def capture(task, *, args, correlation_id, source):
        queued.update(
            task=task,
            args=args,
            correlation_id=correlation_id,
            source=source,
        )

    monkeypatch.setattr(queue_adapter, "enqueue_task", capture)

    record = webhooks.queue_webhook_test_delivery(
        db_session,
        endpoint_id=str(endpoint.id),
    )

    assert db_session.get(IntegrationDelivery, record.id) is not None
    assert record.state == "pending"
    assert (
        queued["task"].name
        == "app.tasks.integration_delivery.deliver_integration_event"
    )
    assert queued["args"] == [str(record.id)]
