"""Celery tasks for webhook delivery.

Handles asynchronous HTTP delivery of webhook events with retry logic.
"""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

import httpx

from app.celery_app import celery_app
from app.models.webhook import WebhookDelivery, WebhookDeliveryStatus
from app.services.credential_crypto import decrypt_credential
from app.services.db_session_adapter import db_session_adapter
from app.services.queue_adapter import enqueue_task

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 10
# Exponential backoff: 1min, 2min, 4min, 8min, 16min, 32min, ~1hr, ~2hr, ~4hr, ~8hr
RETRY_DELAYS = [60, 120, 240, 480, 960, 1920, 3600, 7200, 14400, 28800]


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
                delivery.status = WebhookDeliveryStatus.failed
                delivery.error = "Endpoint not found"
                return

            if not endpoint.is_active:
                logger.info("Endpoint %s is inactive, skipping delivery", endpoint.id)
                delivery.status = WebhookDeliveryStatus.failed
                delivery.error = "Endpoint is inactive"
                return

            payload_json = json.dumps(delivery.payload or {})
            headers = {
                "Content-Type": "application/json",
                "X-Webhook-Event": delivery.event_type.value,
                "X-Webhook-Delivery-Id": str(delivery.id),
            }

            if endpoint.secret:
                plaintext_secret = decrypt_credential(endpoint.secret) or endpoint.secret
                signature = _compute_signature(payload_json, plaintext_secret)
                headers["X-Webhook-Signature-256"] = f"sha256={signature}"

            delivery.last_attempt_at = datetime.now(UTC)
            endpoint_url = endpoint.url
            attempt = delivery.attempt_count + 1
            session.commit()

        logger.info("Delivering webhook to %s (attempt %s)", endpoint_url, attempt)
        with httpx.Client(timeout=30.0) as client:
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
            delivery.response_status = response.status_code

            if response.is_success:
                delivery.status = WebhookDeliveryStatus.delivered
                delivery.delivered_at = datetime.now(UTC)
                delivery.error = None
                logger.info(
                    "Webhook delivered successfully to %s (status %s)",
                    endpoint_url,
                    response.status_code,
                )
            else:
                delivery.attempt_count += 1
                error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
                delivery.error = error_msg
                logger.warning("Webhook delivery failed to %s: %s", endpoint_url, error_msg)

                if delivery.attempt_count < MAX_RETRIES:
                    retry_delay = RETRY_DELAYS[
                        min(delivery.attempt_count - 1, len(RETRY_DELAYS) - 1)
                    ]
                    session.commit()
                    raise self.retry(countdown=retry_delay)
                delivery.status = WebhookDeliveryStatus.failed
                logger.error("Webhook delivery exhausted retries to %s", endpoint_url)

    except (httpx.RequestError, httpx.TimeoutException) as exc:
        # Network/timeout error - update delivery and retry
        try:
            with db_session_adapter.session() as session:
                delivery = session.get(WebhookDelivery, delivery_id)
                if delivery:
                    delivery.attempt_count += 1
                    delivery.last_attempt_at = datetime.now(UTC)
                    delivery.error = str(exc)

                    if delivery.attempt_count >= MAX_RETRIES:
                        delivery.status = WebhookDeliveryStatus.failed
                        logger.error(
                            "Webhook delivery exhausted retries for %s: %s",
                            delivery_id,
                            exc,
                        )
        except Exception:
            logger.exception("Failed to update webhook delivery failure state")

        # Re-raise for Celery retry
        raise

    except Exception as exc:
        logger.exception("Unexpected error delivering webhook %s: %s", delivery_id, exc)
        try:
            with db_session_adapter.session() as session:
                delivery = session.get(WebhookDelivery, delivery_id)
                if delivery:
                    delivery.status = WebhookDeliveryStatus.failed
                    delivery.error = str(exc)
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
        # Find failed deliveries that might be retried
        # (failed but with fewer than max attempts)
        failed_deliveries = (
            session.query(WebhookDelivery)
            .filter(WebhookDelivery.status == WebhookDeliveryStatus.failed)
            .filter(WebhookDelivery.attempt_count < MAX_RETRIES)
            .limit(100)
            .all()
        )

        requeued = 0
        for delivery in failed_deliveries:
            # Reset status to pending and requeue
            delivery.status = WebhookDeliveryStatus.pending
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
