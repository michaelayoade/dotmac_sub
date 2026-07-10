"""Inbound webhook receiver for DotMac Omni CRM events.

The CRM's webhook delivery task POSTs the raw JSON event payload with:
  X-Webhook-Event:          event type (e.g. "ticket.created")
  X-Webhook-Delivery-Id:    CRM delivery UUID
  X-Webhook-Signature-256:  "sha256=" + HMAC-SHA256(raw body, endpoint secret)

Ticket events enqueue a single-ticket sync on the crm queue, so new CRM
tickets appear locally in seconds instead of waiting for the 5-minute pull.
Updates/comments have no CRM webhook events and remain covered by the pull.

Mounted with no router-level auth (see main.py) — authentication is the HMAC
signature, fail-closed: unconfigured secret → 503,
bad/missing signature → 401, compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.crm_webhook_delivery import CrmWebhookDelivery
from app.services import (
    projects_mirror,
    quotes_mirror,
    referrals_mirror,
    work_orders_mirror,
)
from app.services.crm_customers import upsert_customer_from_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/crm", tags=["crm-webhook"])

SIGNATURE_HEADER = "X-Webhook-Signature-256"
EVENT_HEADER = "X-Webhook-Event"
DELIVERY_HEADER = "X-Webhook-Delivery-Id"

# Stable namespace for deriving a delivery id from the HMAC signature when the
# CRM sends no X-Webhook-Delivery-Id (the selfcare pushes don't). Deterministic
# across processes, so identical redeliveries map to the same uuid.
_DELIVERY_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://dotmac.io/crm-webhook-delivery"
)


def _delivery_uuid(request: Request) -> uuid.UUID:
    """Stable id for this delivery: the CRM delivery header when present, else a
    deterministic uuid5 of the signature (identical body -> identical signature
    -> same id), so a byte-identical redelivery collides on the dedup PK."""
    raw = (request.headers.get(DELIVERY_HEADER) or "").strip()
    if raw:
        try:
            return uuid.UUID(raw)
        except ValueError:
            pass
    signature = request.headers.get(SIGNATURE_HEADER) or ""
    return uuid.uuid5(_DELIVERY_NAMESPACE, signature)


def _claim_delivery(
    db: Session,
    delivery_id: uuid.UUID,
    event_type: str,
    *,
    event_id: str | None = None,
) -> bool:
    """Record this delivery as processed; return False if already recorded.

    Check-first (the common case for a sequential redelivery), with the unique
    primary key as a concurrency backstop: a simultaneous delivery that loses the
    race raises IntegrityError and is likewise treated as a duplicate. Fails OPEN
    (returns True) on any other store error — a dedup-store outage must never drop
    a real event. Mirrors the idempotency used for CRM payments (C1).
    """
    try:
        if db.get(CrmWebhookDelivery, delivery_id) is not None:
            return False
        db.add(
            CrmWebhookDelivery(
                delivery_id=delivery_id,
                event_id=event_id,
                event_type=event_type or "unknown",
                status="processed",
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except SQLAlchemyError:
        db.rollback()
        logger.exception("crm_webhook_dedup_store_error event=%s", event_type)
        return True


# CRM ticket events that should refresh the local copy.
TICKET_EVENTS = {"ticket.created", "ticket.resolved", "ticket.escalated"}
CUSTOMER_EVENTS = {"customer.accepted"}
CHAT_EVENTS = {"message.outbound"}
REFERRAL_EVENTS = {"referral.captured", "referral.qualified", "referral.rewarded"}
PROJECT_EVENTS = {
    "project.created",
    "project.updated",
    "project.completed",
    "project.canceled",
    "project_task.completed",
    "project_task.updated",
}
WORK_ORDER_EVENTS = {
    "work_order.created",
    "work_order.updated",
    "work_order.dispatched",
    "work_order.completed",
    "work_order.canceled",
}

QUOTE_EVENTS = {
    "quote.created",
    "quote.updated",
    "quote.accepted",
    "quote.rejected",
}


def _verify_signature(
    raw_body: bytes, presented: str | None, secret: str | None = None
) -> None:
    secret = secret if secret is not None else settings.crm_webhook_secret
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


@router.post("/customers")
async def receive_crm_customer(request: Request, db: Session = Depends(get_db)) -> dict:
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in CUSTOMER_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        )

    response = upsert_customer_from_payload(db, payload)
    response["status"] = "ok"
    return response


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


@router.post("/chat")
async def receive_crm_chat_event(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Wake a backgrounded mobile app when an agent replies in a chat.

    The CRM's chat WebSocket only delivers while the app is foregrounded, so this
    signed webhook fans an agent reply out to the subscriber's devices via FCM.
    It carries no authoritative state — the app pulls history with its visitor
    token — so message bodies here are advisory only.
    """
    raw_body = await request.body()
    # Verify with the shared CRM webhook secret — the same secret the selfcare
    # client signs chat pushes with (dotmac_crm selfcare.notify_chat_message),
    # like every other CRM webhook here. Avoids a separate, unconfigured chat
    # secret that silently fails the signature check.
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in CHAT_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    # Dedup: a redelivered chat push must not wake the device twice.
    if not _claim_delivery(db, _delivery_uuid(request), event_type):
        return {"status": "ignored", "reason": "duplicate", "event": event_type}

    # The CRM wraps event data under "payload" (event envelope); tolerate a flat
    # body too so the contract isn't brittle.
    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    from app.services import push as push_service

    preview = str(body.get("preview") or "").strip() or "You have a new message."
    conversation_id = str(body.get("conversation_id") or "")

    def _wake(sid: str) -> None:
        push_service.send_push(
            db,
            sid,
            title="New message from support",
            body=preview,
            data={"type": "chat_message", "conversation_id": conversation_id},
        )

    subscriber_id = str(body.get("subscriber_id") or "").strip()
    if subscriber_id:
        _wake(subscriber_id)
        return {"status": "ok", "event": event_type}

    # Reseller-originated chat: the reseller org isn't a subscriber, but each
    # active reseller-portal user is backed by a subscriber_id under which its
    # device tokens register — wake all of them (best-effort; no-op when none
    # have registered a device).
    reseller_id = str(body.get("reseller_id") or "").strip()
    if reseller_id:
        from app.services import reseller_portal

        sub_ids = reseller_portal.portal_user_subscriber_ids(db, reseller_id)
        for sid in sub_ids:
            _wake(sid)
        return {"status": "ok" if sub_ids else "ignored", "event": event_type}

    return {"status": "ignored", "reason": "no_target"}


@router.post("/referrals")
async def receive_crm_referral_event(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Apply a CRM referral lifecycle event to the local mirror (RFC #73).

    Handles ``referral.captured`` / ``referral.qualified`` / ``referral.rewarded``;
    rewarded also posts an account credit (idempotent on the referral id via
    ``external_ref``). HMAC-gated; the service acks unmapped/incomplete events so
    the CRM doesn't retry forever. All DB/CRM logic lives in the service.
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in REFERRAL_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    # Tolerate both the CRM event envelope ({"payload": {...}}) and a flat body.
    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    # Dedup: a redelivered referral event must not re-fire the "reward added"
    # push. The credit itself is already idempotent on the referral id.
    if not _claim_delivery(db, _delivery_uuid(request), event_type):
        return {"status": "ignored", "reason": "duplicate", "event": event_type}

    return referrals_mirror.apply_webhook(db, event_type, body)


@router.post("/projects")
async def receive_crm_project_event(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Mirror a CRM project lifecycle event for the installation tracker.

    Handles ``project.created/updated/completed/canceled`` and
    ``project_task.completed/updated``. HMAC-gated; the service acks
    unmapped/incomplete events. All DB/CRM logic lives in the service.
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in PROJECT_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    # Dedup: a redelivered lifecycle event must not re-fire the customer push
    # or re-apply the delta (a redelivery of an older event can otherwise revert
    # a newer status). The periodic reconcile is the backstop for the rare
    # claim-then-fail case.
    if not _claim_delivery(db, _delivery_uuid(request), event_type):
        return {"status": "ignored", "reason": "duplicate", "event": event_type}

    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    return projects_mirror.apply_webhook(db, event_type, body)


@router.post("/work-orders")
async def receive_crm_work_order_event(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Mirror a CRM work-order lifecycle event for the field-service tracker.

    Handles ``work_order.created/updated/dispatched/completed/canceled``.
    HMAC-gated; the service acks unmapped/incomplete events. Logic lives in the
    service.
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in WORK_ORDER_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    # Dedup: a redelivered lifecycle event must not re-fire the customer push
    # or re-apply the delta (a redelivery of an older event can otherwise revert
    # a newer status). The periodic reconcile is the backstop for the rare
    # claim-then-fail case.
    if not _claim_delivery(db, _delivery_uuid(request), event_type):
        return {"status": "ignored", "reason": "duplicate", "event": event_type}

    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    return work_orders_mirror.apply_webhook(db, event_type, body)


@router.post("/quotes")
async def receive_crm_quote_event(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    """Mirror a CRM self-serve quote lifecycle event (Sales/Quotes tracker).

    Handles ``quote.created/updated/accepted/rejected``. HMAC-gated; the service
    acks unmapped/incomplete events. Logic lives in the service.
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type and event_type not in QUOTE_EVENTS:
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    # Dedup: a redelivered lifecycle event must not re-fire the customer push
    # or re-apply the delta (a redelivery of an older event can otherwise revert
    # a newer status). The periodic reconcile is the backstop for the rare
    # claim-then-fail case.
    if not _claim_delivery(db, _delivery_uuid(request), event_type):
        return {"status": "ignored", "reason": "duplicate", "event": event_type}

    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    return quotes_mirror.apply_webhook(db, event_type, body)
