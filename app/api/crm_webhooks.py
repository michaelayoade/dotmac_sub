"""Inbound webhook receiver for DotMac Omni CRM events.

The CRM's webhook delivery task POSTs the raw JSON event payload with:
  X-Webhook-Event:          event type (e.g. "ticket.created")
  X-Webhook-Delivery-Id:    CRM delivery UUID
  X-Webhook-Signature-256:  "sha256=" + HMAC-SHA256(raw body, endpoint secret)

Ticket events enqueue a single-ticket sync on the crm queue, so new CRM
tickets appear locally in seconds instead of waiting for the 5-minute pull.
Updates/comments have no CRM webhook events and remain covered by the pull.

Mounted with no router-level auth (see main.py) — authentication is the HMAC
signature, fail-closed like the Zabbix webhook: unconfigured secret → 503,
bad/missing signature → 401, compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/crm", tags=["crm-webhook"])

SIGNATURE_HEADER = "X-Webhook-Signature-256"
EVENT_HEADER = "X-Webhook-Event"

# CRM ticket events that should refresh the local copy.
TICKET_EVENTS = {"ticket.created", "ticket.resolved", "ticket.escalated"}


def _verify_signature(raw_body: bytes, presented: str | None) -> None:
    secret = settings.crm_webhook_secret
    if not secret:
        logger.error("crm_webhook_secret_not_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM webhook authentication is not configured.",
        )
    expected = (
        "sha256="
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing CRM webhook signature.",
        )


@router.post("")
async def receive_crm_event(request: Request) -> dict:
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    if event_type not in TICKET_EVENTS:
        # Acknowledge so the CRM doesn't retry events we don't consume.
        return {"status": "ignored", "event": event_type}

    ticket_id = str(payload.get("ticket_id") or "").strip()
    if not ticket_id:
        logger.warning("crm_webhook_missing_ticket_id event=%s", event_type)
        return {"status": "ignored", "event": event_type}

    from app.services.queue_adapter import enqueue_task
    from app.tasks.crm_ticket_pull import sync_crm_ticket

    delivery_id = request.headers.get("X-Webhook-Delivery-Id") or ticket_id
    try:
        enqueue_task(
            sync_crm_ticket,
            args=[ticket_id],
            correlation_id=f"crm_webhook:{delivery_id}",
            source="crm_webhook",
        )
    except Exception as exc:  # noqa: BLE001
        # 5xx so the CRM's delivery task retries with backoff.
        logger.error("crm_webhook_enqueue_failed ticket=%s: %s", ticket_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to queue ticket sync.",
        ) from exc

    return {"status": "queued", "event": event_type, "ticket_id": ticket_id}
