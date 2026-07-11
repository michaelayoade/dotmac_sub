from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEventType,
    WebhookSubscription,
)
from app.schemas.webhook import WebhookDeliveryCreate, WebhookDeliveryUpdate
from app.services.common import coerce_uuid
from app.services.events.types import Event

logger = logging.getLogger(__name__)


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
