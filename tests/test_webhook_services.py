import pytest
from fastapi import HTTPException

from app.models.webhook import WebhookDeliveryStatus, WebhookEventType
from app.schemas.webhook import (
    WebhookDeliveryCreate,
    WebhookDeliveryUpdate,
    WebhookEndpointCreate,
    WebhookEndpointUpdate,
    WebhookSubscriptionCreate,
)
from app.services import webhook as webhook_service


def test_webhook_endpoint_subscription_delivery_flow(db_session):
    endpoint = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(
            name="Core Webhooks",
            url="https://example.com/webhooks",
            secret="secret-token",
        ),
    )
    subscriptions = webhook_service.webhook_subscriptions.create(
        db_session,
        WebhookSubscriptionCreate(
            endpoint_id=endpoint.id,
            event_type=WebhookEventType.subscriber_created,
        ),
    )
    delivery = webhook_service.webhook_deliveries.create(
        db_session,
        WebhookDeliveryCreate(
            subscription_id=subscriptions.id,
            event_type=WebhookEventType.subscriber_created,
            payload={"id": "sub-1"},
        ),
    )
    updated = webhook_service.webhook_deliveries.update(
        db_session,
        str(delivery.id),
        WebhookDeliveryUpdate(
            status=WebhookDeliveryStatus.delivered, response_status=200
        ),
    )
    assert updated.status == WebhookDeliveryStatus.delivered


def test_webhook_delivery_logs_structured_lifecycle(db_session, caplog):
    endpoint = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(
            name="Core Webhooks",
            url="https://example.com/webhooks",
            secret="secret-token",
        ),
    )
    subscription = webhook_service.webhook_subscriptions.create(
        db_session,
        WebhookSubscriptionCreate(
            endpoint_id=endpoint.id,
            event_type=WebhookEventType.subscriber_created,
        ),
    )

    caplog.set_level("INFO")
    delivery = webhook_service.webhook_deliveries.create(
        db_session,
        WebhookDeliveryCreate(
            subscription_id=subscription.id,
            event_type=WebhookEventType.subscriber_created,
            payload={"id": "sub-1"},
        ),
    )
    webhook_service.webhook_deliveries.update(
        db_session,
        str(delivery.id),
        WebhookDeliveryUpdate(
            status=WebhookDeliveryStatus.delivered,
            response_status=200,
        ),
    )

    created_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "webhook_delivery_created"
    )
    updated_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "webhook_delivery_updated"
    )

    assert created_record.event == "webhook_delivery"
    assert created_record.delivery_id == str(delivery.id)
    assert updated_record.delivery_status == WebhookDeliveryStatus.delivered.value
    assert updated_record.response_status == 200


def test_webhook_endpoints_default_active(db_session):
    active = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(name="Active", url="https://example.com/active"),
    )
    inactive = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(name="Inactive", url="https://example.com/inactive"),
    )
    webhook_service.webhook_endpoints.update(
        db_session, str(inactive.id), WebhookEndpointUpdate(is_active=False)
    )
    items = webhook_service.webhook_endpoints.list(
        db_session,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    ids = {item.id for item in items}
    assert active.id in ids
    assert inactive.id not in ids


def test_webhook_subscription_list_invalid_event(db_session):
    with pytest.raises(HTTPException) as exc:
        webhook_service.webhook_subscriptions.list(
            db_session,
            endpoint_id=None,
            event_type="bad.event",
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400
