from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    WebhookEventType,
    WebhookSubscription,
)
from app.schemas.webhook import WebhookDeliveryCreate, WebhookDeliveryUpdate
from app.services.common import coerce_uuid
from app.services.events.types import Event

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 10
MAX_RETRIES = 20
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 30
MAX_DELIVERY_TIMEOUT_SECONDS = 300
MAX_RETRY_DELAY_SECONDS = 28800
# Exponential backoff: 1min, 2min, 4min, 8min, 16min, 32min, ~1hr, ~2hr, ~4hr, ~8hr
RETRY_DELAYS = [60, 120, 240, 480, 960, 1920, 3600, 7200, 14400, 28800]


def delivery_extra(delivery: WebhookDelivery) -> dict[str, object]:
    return {
        "event": "webhook_delivery",
        "delivery_id": str(delivery.id),
        "subscription_id": str(delivery.subscription_id),
        "endpoint_id": str(delivery.endpoint_id),
        "event_type": delivery.event_type.value if delivery.event_type else None,
        "delivery_status": delivery.status.value if delivery.status else None,
        "response_status": delivery.response_status,
    }


def create_manual_delivery(
    db: Session,
    payload: WebhookDeliveryCreate,
) -> WebhookDelivery:
    subscription = db.get(WebhookSubscription, coerce_uuid(payload.subscription_id))
    if not subscription:
        raise HTTPException(status_code=404, detail="Webhook subscription not found")
    delivery = WebhookDelivery(
        subscription_id=subscription.id,
        endpoint_id=subscription.endpoint_id,
        event_type=payload.event_type,
        status=WebhookDeliveryStatus.pending,
        payload=payload.payload,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    logger.info("webhook_delivery_created", extra=delivery_extra(delivery))
    return delivery


def update_delivery(
    db: Session,
    delivery_id: str,
    payload: WebhookDeliveryUpdate,
) -> WebhookDelivery:
    delivery = db.get(WebhookDelivery, coerce_uuid(delivery_id))
    if not delivery:
        raise HTTPException(status_code=404, detail="Webhook delivery not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(delivery, key, value)
    db.commit()
    db.refresh(delivery)
    logger.info("webhook_delivery_updated", extra=delivery_extra(delivery))
    return delivery


def create_for_event(
    db: Session,
    *,
    event: Event,
    webhook_event_type: WebhookEventType,
) -> list[WebhookDelivery]:
    subscriptions = list(
        db.scalars(
            select(WebhookSubscription)
            .where(WebhookSubscription.event_type == webhook_event_type)
            .where(WebhookSubscription.is_active.is_(True))
        ).all()
    )
    deliveries: list[WebhookDelivery] = []
    for subscription in subscriptions:
        if not subscription.endpoint or not subscription.endpoint.is_active:
            logger.debug(
                "Skipping inactive endpoint for subscription %s",
                subscription.id,
            )
            continue
        delivery = WebhookDelivery(
            subscription_id=subscription.id,
            endpoint_id=subscription.endpoint_id,
            event_type=webhook_event_type,
            status=WebhookDeliveryStatus.pending,
            payload=event.to_dict(),
        )
        db.add(delivery)
        db.flush()
        deliveries.append(delivery)
    return deliveries


def endpoint_max_retries(endpoint: WebhookEndpoint | None) -> int:
    configured = getattr(endpoint, "max_retries", None)
    if configured is None:
        return DEFAULT_MAX_RETRIES
    return max(0, min(MAX_RETRIES, int(configured)))


def endpoint_timeout_seconds(endpoint: WebhookEndpoint | None) -> float:
    configured = getattr(endpoint, "delivery_timeout_seconds", None)
    if configured is None:
        return float(DEFAULT_DELIVERY_TIMEOUT_SECONDS)
    return float(max(1, min(MAX_DELIVERY_TIMEOUT_SECONDS, int(configured))))


def endpoint_retry_delay(endpoint: WebhookEndpoint | None, attempt_count: int) -> int:
    configured = getattr(endpoint, "retry_backoff_seconds", None)
    if configured is not None:
        base = max(1, min(MAX_RETRY_DELAY_SECONDS, int(configured)))
        exponent = max(0, attempt_count - 1)
        return min(MAX_RETRY_DELAY_SECONDS, base * (2**exponent))
    return RETRY_DELAYS[min(max(attempt_count - 1, 0), len(RETRY_DELAYS) - 1)]


def delivery_can_retry(delivery: WebhookDelivery) -> bool:
    return delivery.attempt_count < endpoint_max_retries(delivery.endpoint)


def mark_delivery_missing_endpoint(delivery: WebhookDelivery) -> None:
    delivery.status = WebhookDeliveryStatus.failed
    delivery.error = "Endpoint not found"


def mark_delivery_inactive_endpoint(delivery: WebhookDelivery) -> None:
    delivery.status = WebhookDeliveryStatus.failed
    delivery.error = "Endpoint is inactive"


def mark_delivery_attempt_started(
    delivery: WebhookDelivery,
    *,
    attempted_at: datetime | None = None,
) -> None:
    delivery.last_attempt_at = attempted_at or datetime.now(UTC)


def mark_delivery_delivered(
    delivery: WebhookDelivery,
    *,
    response_status: int,
    delivered_at: datetime | None = None,
) -> None:
    delivery.response_status = response_status
    delivery.status = WebhookDeliveryStatus.delivered
    delivery.delivered_at = delivered_at or datetime.now(UTC)
    delivery.error = None


def record_delivery_http_failure(
    delivery: WebhookDelivery,
    *,
    response_status: int,
    response_text: str,
) -> int | None:
    delivery.response_status = response_status
    delivery.attempt_count += 1
    delivery.error = f"HTTP {response_status}: {response_text[:500]}"
    max_retries = endpoint_max_retries(delivery.endpoint)
    if delivery.attempt_count < max_retries:
        return endpoint_retry_delay(delivery.endpoint, delivery.attempt_count)
    delivery.status = WebhookDeliveryStatus.failed
    return None


def record_delivery_transport_failure(
    delivery: WebhookDelivery,
    *,
    error: Exception,
    attempted_at: datetime | None = None,
) -> int | None:
    delivery.attempt_count += 1
    delivery.last_attempt_at = attempted_at or datetime.now(UTC)
    delivery.error = str(error)
    max_retries = endpoint_max_retries(delivery.endpoint)
    if delivery.attempt_count >= max_retries:
        delivery.status = WebhookDeliveryStatus.failed
        return None
    return endpoint_retry_delay(delivery.endpoint, delivery.attempt_count)


def mark_delivery_unexpected_failure(
    delivery: WebhookDelivery,
    *,
    error: Exception,
) -> None:
    delivery.status = WebhookDeliveryStatus.failed
    delivery.error = str(error)


def list_retryable_failed_deliveries(
    db: Session,
    *,
    limit: int = 100,
) -> list[WebhookDelivery]:
    candidates = list(
        db.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == WebhookDeliveryStatus.failed)
            .where(WebhookDelivery.attempt_count < MAX_RETRIES)
            .limit(limit * 5)
        ).all()
    )
    return [delivery for delivery in candidates if delivery_can_retry(delivery)][:limit]


def mark_delivery_pending_for_retry(delivery: WebhookDelivery) -> None:
    delivery.status = WebhookDeliveryStatus.pending


def queue_deliveries(
    deliveries: list[WebhookDelivery],
    *,
    event: Event,
) -> None:
    if not deliveries:
        return
    from app.services.queue_adapter import enqueue_task
    from app.tasks.webhooks import deliver_webhook

    for delivery in deliveries:
        enqueue_task(
            deliver_webhook,
            args=[str(delivery.id)],
            correlation_id=f"webhook_event:{event.event_id}",
            source="event_webhook_handler",
        )
