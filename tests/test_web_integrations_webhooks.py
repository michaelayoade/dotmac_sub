from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.models.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEventType,
    WebhookSubscription,
)
from app.services import web_integrations
from app.services.credential_crypto import decrypt_credential, is_encrypted


def test_integrations_webhook_create_encrypts_secret_and_subscribes_events(db_session):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret="partner-secret",
        event_types=["subscriber.created", "invoice.paid"],
        is_active=True,
    )

    assert endpoint.secret != "partner-secret"
    assert is_encrypted(endpoint.secret)
    assert decrypt_credential(endpoint.secret) == "partner-secret"
    subscriptions = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .all()
    )
    assert {subscription.event_type for subscription in subscriptions} == {
        WebhookEventType.subscriber_created,
        WebhookEventType.invoice_paid,
    }


def test_integrations_webhook_update_preserves_secret_and_resyncs_events(db_session):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret="partner-secret",
        event_types=["subscriber.created", "invoice.paid"],
        is_active=True,
    )
    original_secret = endpoint.secret

    updated = web_integrations.update_webhook_endpoint(
        db_session,
        endpoint_id=str(endpoint.id),
        name="Partner webhook updated",
        url=endpoint.url,
        connector_config_id=None,
        secret="",
        event_types=["invoice.paid", "network.alert"],
        is_active=False,
    )

    assert updated.secret == original_secret
    assert updated.is_active is False
    assert updated.name == "Partner webhook updated"
    subscriptions = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .all()
    )
    active_events = {
        subscription.event_type
        for subscription in subscriptions
        if subscription.is_active
    }
    assert active_events == {
        WebhookEventType.invoice_paid,
        WebhookEventType.network_alert,
    }


def test_integrations_webhook_rotate_secret_changes_encrypted_value(db_session):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret="partner-secret",
        event_types=["subscriber.created"],
        is_active=True,
    )
    original_secret = endpoint.secret

    rotated = web_integrations.rotate_webhook_endpoint_secret(
        db_session,
        endpoint_id=str(endpoint.id),
    )

    assert rotated.secret != original_secret
    assert is_encrypted(rotated.secret)
    assert decrypt_credential(rotated.secret) != "partner-secret"


def test_integrations_webhook_test_delivery_queues_first_active_subscription(
    db_session, monkeypatch
):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret="partner-secret",
        event_types=["invoice.paid"],
        is_active=True,
    )
    queued = {}

    def fake_enqueue_task(task_name, *, args, correlation_id, source):
        queued["task_name"] = task_name
        queued["args"] = args
        queued["correlation_id"] = correlation_id
        queued["source"] = source

    monkeypatch.setattr(web_integrations, "enqueue_task", fake_enqueue_task)

    delivery = web_integrations.queue_webhook_test_delivery(
        db_session,
        endpoint_id=str(endpoint.id),
    )

    assert db_session.get(WebhookDelivery, delivery.id) is not None
    assert delivery.event_type == WebhookEventType.invoice_paid
    assert queued["task_name"] == "app.tasks.webhooks.deliver_webhook"
    assert queued["args"] == [str(delivery.id)]
    assert queued["source"] == "admin_integrations_webhook_test"


def test_integrations_webhook_detail_includes_delivery_summary(db_session):
    endpoint = web_integrations.create_webhook_endpoint(
        db_session,
        name="Partner webhook",
        url=f"https://example.com/{uuid4()}",
        connector_config_id=None,
        secret=None,
        event_types=["invoice.paid"],
        is_active=True,
    )
    subscription = (
        db_session.query(WebhookSubscription)
        .filter(WebhookSubscription.endpoint_id == endpoint.id)
        .one()
    )
    now = datetime.now(UTC)
    delivered = WebhookDelivery(
        subscription_id=subscription.id,
        endpoint_id=endpoint.id,
        event_type=WebhookEventType.invoice_paid,
        status=WebhookDeliveryStatus.delivered,
        attempt_count=1,
        response_status=200,
        delivered_at=now - timedelta(minutes=10),
        created_at=now - timedelta(minutes=10),
    )
    failed = WebhookDelivery(
        subscription_id=subscription.id,
        endpoint_id=endpoint.id,
        event_type=WebhookEventType.invoice_paid,
        status=WebhookDeliveryStatus.failed,
        attempt_count=2,
        response_status=500,
        error="HTTP 500: upstream failed",
        last_attempt_at=now,
        created_at=now,
    )
    db_session.add_all([delivered, failed])
    db_session.commit()

    state = web_integrations.build_webhook_detail_data(
        db_session, endpoint_id=str(endpoint.id)
    )

    summary = state["delivery_summary"]
    assert summary["latest_delivery"].id == failed.id
    assert summary["latest_failure"].id == failed.id
    assert summary["delivered_count"] == 1
    assert summary["failed_count"] == 1
    assert summary["pending_count"] == 0


def test_integrations_webhook_templates_do_not_render_secret_values():
    new_template = Path("templates/admin/integrations/webhooks/new.html").read_text()
    detail_template = Path(
        "templates/admin/integrations/webhooks/detail.html"
    ).read_text()
    index_template = Path("templates/admin/integrations/webhooks/index.html").read_text()

    assert 'type="password" name="secret"' in new_template
    assert "form.secret" not in new_template
    assert "Leave blank to keep current secret" in new_template
    assert "endpoint.secret[-4:]" not in detail_template
    assert "Configured" in detail_template
    assert "/edit" in index_template
    assert "/rotate-secret" in detail_template
    assert "/test" in detail_template
    assert "/disable" in detail_template
    assert "/delete" in detail_template
    assert "Latest delivery" in detail_template
    assert "Latest failure" in detail_template


def test_connector_detail_exposes_check_connection_action():
    template = Path("templates/admin/integrations/connectors/detail.html").read_text()

    assert "Check Connection" in template
    assert "/embed?check=1" in template
