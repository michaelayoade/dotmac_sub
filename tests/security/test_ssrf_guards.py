from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.webhook import WebhookDelivery, WebhookDeliveryStatus, WebhookEventType
from app.schemas.webhook import (
    WebhookDeliveryCreate,
    WebhookEndpointCreate,
    WebhookSubscriptionCreate,
)
from app.services import sms as sms_service
from app.services import webhook as webhook_service
from app.tasks import webhooks as webhook_tasks


BLOCKED_WEBHOOK_ENDPOINTS = (
    "https://169.254.169.254/hook",
    "https://10.0.0.1/hook",
    "https://localhost/hook",
)

BLOCKED_SMS_WEBHOOK_URLS = (
    "http://169.254.169.254/",
    "http://10.0.0.1/",
    "http://localhost/",
)


def _create_pending_delivery(db_session, endpoint_url: str) -> str:
    endpoint = webhook_service.webhook_endpoints.create(
        db_session,
        WebhookEndpointCreate(
            name=f"Security Endpoint {uuid.uuid4().hex}",
            url=endpoint_url,
        ),
    )
    subscription = webhook_service.webhook_subscriptions.create(
        db_session,
        WebhookSubscriptionCreate(
            endpoint_id=endpoint.id,
            event_type=WebhookEventType.subscriber_created,
        ),
    )
    delivery = webhook_service.webhook_deliveries.create(
        db_session,
        WebhookDeliveryCreate(
            subscription_id=subscription.id,
            event_type=WebhookEventType.subscriber_created,
            payload={"id": "sub-1"},
        ),
    )
    return str(delivery.id)


@pytest.mark.parametrize("blocked_url", BLOCKED_WEBHOOK_ENDPOINTS)
def test_webhook_delivery_rejects_private_loopback_and_link_local_targets(
    db_session,
    monkeypatch,
    blocked_url: str,
):
    test_session_factory = sessionmaker(
        bind=db_session.get_bind(),
        autoflush=False,
        autocommit=False,
    )
    monkeypatch.setattr(webhook_tasks, "SessionLocal", test_session_factory)

    delivery_id = _create_pending_delivery(db_session, blocked_url)

    with patch("app.tasks.webhooks.httpx.Client") as mock_client:
        webhook_tasks.deliver_webhook(delivery_id)

    mock_client.assert_not_called()
    updated = db_session.get(WebhookDelivery, uuid.UUID(delivery_id))
    assert updated is not None
    assert updated.status == WebhookDeliveryStatus.failed
    assert updated.error == "SSRF blocked"
    assert updated.attempt_count == 1


@pytest.mark.parametrize("blocked_url", BLOCKED_SMS_WEBHOOK_URLS)
def test_sms_webhook_sender_rejects_private_loopback_and_link_local_targets(
    blocked_url: str,
):
    with patch("app.services.sms.httpx.post") as mock_post:
        success, external_id, error = sms_service._send_via_webhook(
            blocked_url,
            api_key="test-api-key",
            to_phone="+15550001111",
            body="hello",
        )

    mock_post.assert_not_called()
    assert success is False
    assert external_id is None
    assert error is not None
    assert "ssrf" in error.lower()
