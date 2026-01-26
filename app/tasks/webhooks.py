"""Celery tasks for webhook delivery.

Handles asynchronous HTTP delivery of webhook events with retry logic.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx

from app.celery_app import celery_app
from app.db import SessionLocal
from app.models.webhook import WebhookDelivery, WebhookDeliveryStatus
from app.schemas.crm.inbox import EmailWebhookPayload, MetaWebhookPayload, WhatsAppWebhookPayload
from app.services import crm as crm_service
from app.services import meta_webhooks

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
    session = SessionLocal()
    try:
        delivery = session.get(WebhookDelivery, delivery_id)
        if not delivery:
            logger.error(f"WebhookDelivery not found: {delivery_id}")
            return

        # Get endpoint
        endpoint = delivery.endpoint
        if not endpoint:
            logger.error(f"Endpoint not found for delivery {delivery_id}")
            delivery.status = WebhookDeliveryStatus.failed
            delivery.error = "Endpoint not found"
            session.commit()
            return

        if not endpoint.is_active:
            logger.info(f"Endpoint {endpoint.id} is inactive, skipping delivery")
            delivery.status = WebhookDeliveryStatus.failed
            delivery.error = "Endpoint is inactive"
            session.commit()
            return

        # Prepare payload
        payload_json = json.dumps(delivery.payload or {})

        # Build headers
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Event": delivery.event_type.value,
            "X-Webhook-Delivery-Id": str(delivery.id),
        }

        # Add signature if secret is configured
        if endpoint.secret:
            signature = _compute_signature(payload_json, endpoint.secret)
            headers["X-Webhook-Signature-256"] = f"sha256={signature}"

        # Update attempt timestamp only - count is incremented on failure
        delivery.last_attempt_at = datetime.now(timezone.utc)
        session.commit()

        # Make HTTP request
        logger.info(
            f"Delivering webhook to {endpoint.url} "
            f"(attempt {delivery.attempt_count + 1})"
        )

        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                endpoint.url,
                content=payload_json,
                headers=headers,
            )

        # Record response
        delivery.response_status = response.status_code

        if response.is_success:
            delivery.status = WebhookDeliveryStatus.delivered
            delivery.delivered_at = datetime.now(timezone.utc)
            delivery.error = None
            logger.info(
                f"Webhook delivered successfully to {endpoint.url} "
                f"(status {response.status_code})"
            )
        else:
            # Increment attempt count on failure
            delivery.attempt_count += 1
            error_msg = f"HTTP {response.status_code}: {response.text[:500]}"
            delivery.error = error_msg
            logger.warning(
                f"Webhook delivery failed to {endpoint.url}: {error_msg}"
            )

            # Retry if we have attempts remaining
            if delivery.attempt_count < MAX_RETRIES:
                retry_delay = RETRY_DELAYS[min(delivery.attempt_count - 1, len(RETRY_DELAYS) - 1)]
                session.commit()
                raise self.retry(countdown=retry_delay)
            else:
                delivery.status = WebhookDeliveryStatus.failed
                logger.error(
                    f"Webhook delivery exhausted retries to {endpoint.url}"
                )

        session.commit()

    except (httpx.RequestError, httpx.TimeoutException) as exc:
        # Network/timeout error - update delivery and retry
        try:
            delivery = session.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.attempt_count += 1
                delivery.last_attempt_at = datetime.now(timezone.utc)
                delivery.error = str(exc)

                if delivery.attempt_count >= MAX_RETRIES:
                    delivery.status = WebhookDeliveryStatus.failed
                    logger.error(
                        f"Webhook delivery exhausted retries for {delivery_id}: {exc}"
                    )
                session.commit()
        except Exception:
            session.rollback()

        # Re-raise for Celery retry
        raise

    except Exception as exc:
        logger.exception(f"Unexpected error delivering webhook {delivery_id}: {exc}")
        try:
            delivery = session.get(WebhookDelivery, delivery_id)
            if delivery:
                delivery.status = WebhookDeliveryStatus.failed
                delivery.error = str(exc)
                session.commit()
        except Exception:
            session.rollback()
        raise

    finally:
        session.close()


@celery_app.task(name="app.tasks.webhooks.retry_failed_deliveries")
def retry_failed_deliveries():
    """Scheduled task to retry failed deliveries that may be recoverable.

    This task finds failed deliveries that haven't exhausted retries
    and re-queues them for delivery.
    """
    session = SessionLocal()
    try:
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
            deliver_webhook.delay(str(delivery.id))
            requeued += 1

        if requeued:
            logger.info(f"Requeued {requeued} failed webhook deliveries")

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(name="app.tasks.webhooks.process_whatsapp_webhook")
def process_whatsapp_webhook(payload: dict):
    session = SessionLocal()
    try:
        parsed = WhatsAppWebhookPayload(**payload)
        crm_service.inbox.receive_whatsapp_message(session, parsed)
    except Exception as exc:
        logger.exception("whatsapp_webhook_processing_failed error=%s", exc)
    finally:
        session.close()


@celery_app.task(name="app.tasks.webhooks.process_email_webhook")
def process_email_webhook(payload: dict):
    session = SessionLocal()
    try:
        parsed = EmailWebhookPayload(**payload)
        crm_service.inbox.receive_email_message(session, parsed)
    except Exception as exc:
        logger.exception("email_webhook_processing_failed error=%s", exc)
    finally:
        session.close()


@celery_app.task(name="app.tasks.webhooks.process_meta_webhook")
def process_meta_webhook(payload: dict):
    session = SessionLocal()
    try:
        parsed = MetaWebhookPayload(**payload)
        if parsed.object == "page":
            meta_webhooks.process_messenger_webhook(session, parsed)
        elif parsed.object == "instagram":
            meta_webhooks.process_instagram_webhook(session, parsed)
        else:
            logger.warning("meta_webhook_unknown_object object=%s", parsed.object)
    except Exception as exc:
        logger.exception("meta_webhook_processing_failed error=%s", exc)
    finally:
        session.close()
