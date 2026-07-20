"""Celery tasks for webhook delivery.

Handles asynchronous HTTP delivery of webhook events with retry logic.
"""

import hashlib
import hmac
import json
import logging

import httpx

from app.celery_app import celery_app
from app.models.webhook import WebhookDelivery
from app.services.credential_crypto import decrypt_credential
from app.services.db_session_adapter import db_session_adapter
from app.services.queue_adapter import enqueue_task
from app.services.webhook_deliveries import (
    MAX_RETRIES,
    endpoint_timeout_seconds,
    list_retryable_failed_deliveries,
    mark_delivery_attempt_started,
    mark_delivery_delivered,
    mark_delivery_inactive_endpoint,
    mark_delivery_missing_endpoint,
    mark_delivery_pending_for_retry,
    mark_delivery_unexpected_failure,
    record_delivery_http_failure,
    record_delivery_transport_failure,
)

logger = logging.getLogger(__name__)


def _compute_signature(payload: str, secret: str) -> str:
    """Compute HMAC-SHA256 signature for payload verification."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@celery_app.task(
    name="app.tasks.webhooks.deliver_webhook",
    bind=True,
    max_retries=MAX_RETRIES,
    autoretry_for=(httpx.RequestError, httpx.TimeoutException),
    retry_backoff=True,
    retry_backoff_max=28800,  # 8 hours max
)
def deliver_webhook(self, delivery_id: str):
    """Deliver a webhook to the configured endpoint.

    This task handles HTTP delivery with:
    - HMAC signature for payload verification
    - Exponential backoff retry on failure
    - Status tracking in WebhookDelivery table

    Args:
        delivery_id: UUID of the WebhookDelivery record
    """
    try:
        with db_session_adapter.session() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            if not delivery:
                logger.error("WebhookDelivery not found: %s", delivery_id)
                return

            endpoint = delivery.endpoint
            if not endpoint:
                logger.error("Endpoint not found for delivery %s", delivery_id)
                mark_delivery_missing_endpoint(delivery)
                return

            if not endpoint.is_active:
                logger.info("Endpoint %s is inactive, skipping delivery", endpoint.id)
                mark_delivery_inactive_endpoint(delivery)
                return

            payload_json = json.dumps(delivery.payload or {})
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Event": delivery.event_type.value,
                "X-Webhook-Delivery-Id": str(delivery.id),
            }

            if endpoint.secret:
                plaintext_secret = (
                    decrypt_credential(endpoint.secret) or endpoint.secret
                )
                signature = _compute_signature(payload_json, plaintext_secret)
                headers["X-Webhook-Signature-256"] = f"sha256={signature}"

            mark_delivery_attempt_started(delivery)
            endpoint_url = endpoint.url
            delivery_timeout = endpoint_timeout_seconds(endpoint)
            attempt = delivery.attempt_count + 1
            session.commit()

        logger.info("Delivering webhook to %s (attempt %s)", endpoint_url, attempt)
        with httpx.Client(timeout=delivery_timeout) as client:
            response = client.post(
                endpoint_url,
                content=payload_json,
                headers=headers,
            )

        with db_session_adapter.session() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            if not delivery:
                logger.error("WebhookDelivery disappeared after send: %s", delivery_id)
                return

            if response.is_success:
                mark_delivery_delivered(
                    delivery,
                    response_status=response.status_code,
                )
                logger.info(
                    "Webhook delivered successfully to %s (status %s)",
                    endpoint_url,
                    response.status_code,
                )
            else:
                retry_delay = record_delivery_http_failure(
                    delivery,
                    response_status=response.status_code,
                    response_text=response.text,
                )
                logger.warning(
                    "Webhook delivery failed to %s: %s",
                    endpoint_url,
                    delivery.error,
                )

                if retry_delay is not None:
                    session.commit()
                    raise self.retry(countdown=retry_delay)
                logger.error("Webhook delivery exhausted retries to %s", endpoint_url)

    except (httpx.RequestError, httpx.TimeoutException) as exc:
        # Network/timeout error - update delivery and retry
        retry_countdown: int | None = None
        try:
            with db_session_adapter.session() as session:
                delivery = session.get(WebhookDelivery, delivery_id)
                if delivery:
                    retry_countdown = record_delivery_transport_failure(
                        delivery,
                        error=exc,
                    )
                    if retry_countdown is None:
                        logger.error(
                            "Webhook delivery exhausted retries for %s: %s",
                            delivery_id,
                            exc,
                        )
        except Exception:
            logger.exception("Failed to update webhook delivery failure state")
        if retry_countdown is not None:
            raise self.retry(countdown=retry_countdown)

    except Exception as exc:
        logger.exception("Unexpected error delivering webhook %s: %s", delivery_id, exc)
        try:
            with db_session_adapter.session() as session:
                delivery = session.get(WebhookDelivery, delivery_id)
                if delivery:
                    mark_delivery_unexpected_failure(delivery, error=exc)
        except Exception:
            logger.exception("Failed to update unexpected webhook failure state")
        raise


@celery_app.task(name="app.tasks.webhooks.retry_failed_deliveries")
def retry_failed_deliveries():
    """Scheduled task to retry failed deliveries that may be recoverable.

    This task finds failed deliveries that haven't exhausted retries
    and re-queues them for delivery.
    """
    with db_session_adapter.session() as session:
        failed_deliveries = list_retryable_failed_deliveries(session, limit=100)

        requeued = 0
        for delivery in failed_deliveries:
            mark_delivery_pending_for_retry(delivery)
            session.commit()
            enqueue_task(
                "app.tasks.webhooks.deliver_webhook",
                args=[str(delivery.id)],
                correlation_id=f"webhook_delivery:{delivery.id}",
                source="retry_failed_deliveries",
            )
            requeued += 1

        if requeued:
            logger.info("Requeued %s failed webhook deliveries", requeued)
