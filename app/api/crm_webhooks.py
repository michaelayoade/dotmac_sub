"""Inbound webhook receiver for DotMac Omni CRM events.

The CRM's webhook delivery task POSTs the raw JSON event payload with:
  X-Webhook-Event:          event type (e.g. "ticket.created")
  X-Webhook-Delivery-Id:    CRM delivery UUID
  X-Webhook-Signature-256:  "sha256=" + HMAC-SHA256(raw body, endpoint secret)

Ticket events are applied directly with delivery-id idempotency, while the
5-minute pull remains as a reconciliation safety net during rollout.

Mounted with no router-level auth (see main.py) — authentication is the HMAC
signature, fail-closed like the Zabbix webhook: unconfigured secret → 503,
bad/missing signature → 401, compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.crm_webhook_delivery import CrmWebhookDelivery
from app.models.subscriber import Subscriber
from app.models.support import Ticket, TicketComment, TicketCommentAuthorType
from app.services import (
    projects_mirror,
    quotes_mirror,
    referrals_mirror,
    work_orders_mirror,
)
from app.services.crm_customers import upsert_customer_from_payload
from app.services.crm_ticket_pull import (
    _apply_ticket_fields,
    _clean_attachments,
    _comment_exists,
    _find_existing_ticket,
    _find_local_subscriber_id_from_ticket_text,
    _parse_datetime,
    load_local_crm_id_map,
    load_local_subscriber_map,
)
from app.services.support import _coerce_uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/crm", tags=["crm-webhook"])

SIGNATURE_HEADER = "X-Webhook-Signature-256"
EVENT_HEADER = "X-Webhook-Event"

# CRM ticket events accepted from the durable CRM webhook feed.
TICKET_EVENTS = {
    "ticket.created",
    "ticket.updated",
    "ticket.resolved",
    "ticket.escalated",
    "ticket.comment_created",
}
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


def _event_type_from_request(request: Request, body: dict[str, Any]) -> str:
    header_event = str(request.headers.get(EVENT_HEADER) or "").strip()
    body_event = str(body.get("event_type") or "").strip()
    if header_event and body_event and header_event != body_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook event header/body mismatch.",
        )
    return header_event or body_event


def _delivery_id_from_request(request: Request) -> UUID:
    delivery_id = str(request.headers.get("X-Webhook-Delivery-Id") or "").strip()
    parsed = _coerce_uuid(delivery_id)
    if not parsed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid X-Webhook-Delivery-Id.",
        )
    return parsed


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _value(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _crm_ticket_id_from_payload(
    event_type: str, envelope: dict[str, Any], payload: dict[str, Any]
) -> str | None:
    context = _as_dict(envelope.get("context"))
    nested_ticket = _as_dict(payload.get("ticket"))
    if event_type == "ticket.comment_created":
        return _value(
            context.get("ticket_id"),
            payload.get("ticket_id"),
            nested_ticket.get("id"),
            nested_ticket.get("ticket_id"),
        )
    return _value(
        context.get("ticket_id"),
        payload.get("ticket_id"),
        payload.get("id"),
        nested_ticket.get("id"),
        nested_ticket.get("ticket_id"),
    )


def _build_ticket_payload(
    event_type: str, envelope: dict[str, Any]
) -> dict[str, Any] | None:
    payload = _as_dict(envelope.get("payload"))
    context = _as_dict(envelope.get("context"))
    ticket_source = _as_dict(payload.get("ticket")) or payload
    crm_ticket_id = _crm_ticket_id_from_payload(event_type, envelope, payload)
    if not crm_ticket_id:
        return None

    crm_subscriber_id = _value(context.get("subscriber_id"))
    updated_at = _value(
        ticket_source.get("updated_at"),
        payload.get("ticket_updated_at"),
        envelope.get("occurred_at"),
    )
    status_value = _value(ticket_source.get("status"))
    if event_type == "ticket.resolved" and not status_value:
        status_value = "resolved"

    return {
        "id": crm_ticket_id,
        "subscriber_id": crm_subscriber_id,
        "number": _value(ticket_source.get("number"), ticket_source.get("ticket_number")),
        "title": _value(ticket_source.get("title"), ticket_source.get("subject"))
        or "CRM ticket",
        "description": ticket_source.get("description") or ticket_source.get("body"),
        "region": ticket_source.get("region"),
        "status": status_value or "open",
        "priority": _value(ticket_source.get("priority"))
        or ("high" if event_type == "ticket.escalated" else "normal"),
        "ticket_type": ticket_source.get("ticket_type") or ticket_source.get("type"),
        "channel": ticket_source.get("channel") or "api",
        "tags": ticket_source.get("tags"),
        "metadata": ticket_source.get("metadata") or ticket_source.get("metadata_"),
        "attachments": ticket_source.get("attachments"),
        "due_at": ticket_source.get("due_at"),
        "resolved_at": ticket_source.get("resolved_at"),
        "closed_at": ticket_source.get("closed_at"),
        "is_active": ticket_source.get("is_active", True),
        "created_at": ticket_source.get("created_at") or envelope.get("occurred_at"),
        "updated_at": updated_at,
        "customer_person_id": ticket_source.get("customer_person_id"),
        "created_by_person_id": ticket_source.get("created_by_person_id"),
        "assigned_to_person_id": ticket_source.get("assigned_to_person_id"),
        "ticket_manager_person_id": ticket_source.get("ticket_manager_person_id"),
        "service_team_id": ticket_source.get("service_team_id"),
    }


def _resolve_subscriber_id(
    db: Session,
    *,
    envelope: dict[str, Any],
    ticket_payload: dict[str, Any],
    existing: Ticket | None = None,
) -> UUID | None:
    if existing and existing.subscriber_id:
        return existing.subscriber_id

    context = _as_dict(envelope.get("context"))
    payload = _as_dict(envelope.get("payload"))
    local_by_crm_id = load_local_crm_id_map(db)
    # Confirmed contract: payload.subscriber_id is Selfcare's external ID.
    # context.subscriber_id is CRM's subscriber UUID and is only a fallback.
    for candidate in (
        context.get("selfcare_subscriber_id"),
        payload.get("selfcare_subscriber_id"),
        payload.get("external_id"),
        payload.get("subscriber_id"),
    ):
        candidate_text = _value(candidate)
        if not candidate_text:
            continue
        parsed = _coerce_uuid(candidate_text)
        if parsed and db.get(Subscriber, parsed):
            return parsed
        if parsed and candidate_text in local_by_crm_id:
            return local_by_crm_id[candidate_text]
        if candidate_text.isdigit():
            subscriber = (
                db.query(Subscriber)
                .filter(Subscriber.splynx_customer_id == int(candidate_text))
                .first()
            )
            if subscriber:
                return subscriber.id

    context_crm_id = _value(context.get("subscriber_id"))
    if context_crm_id and context_crm_id in local_by_crm_id:
        return local_by_crm_id[context_crm_id]

    local_by_splynx = load_local_subscriber_map(db)
    return _find_local_subscriber_id_from_ticket_text(ticket_payload, local_by_splynx)


def _incoming_is_stale(existing: Ticket | None, ticket_payload: dict[str, Any]) -> bool:
    if not existing:
        return False
    incoming = _parse_datetime(ticket_payload.get("updated_at"))
    stored = _parse_datetime((existing.metadata_ or {}).get("crm_updated_at"))
    return bool(incoming and stored and incoming < stored)


def _upsert_ticket_from_webhook(
    db: Session, event_type: str, envelope: dict[str, Any]
) -> dict[str, str | None]:
    ticket_payload = _build_ticket_payload(event_type, envelope)
    if ticket_payload is None:
        return {"result": "ignored_missing_ticket_id", "crm_ticket_id": None}

    existing = _find_existing_ticket(db, ticket_payload)
    if _incoming_is_stale(existing, ticket_payload):
        return {
            "result": "stale_ignored",
            "crm_ticket_id": str(ticket_payload.get("id") or ""),
        }

    subscriber_id = _resolve_subscriber_id(
        db, envelope=envelope, ticket_payload=ticket_payload, existing=existing
    )
    if not subscriber_id:
        return {
            "result": "skipped_unmapped_subscriber",
            "crm_ticket_id": str(ticket_payload.get("id") or ""),
        }

    if existing is None:
        ticket = Ticket(title="CRM ticket")
        db.add(ticket)
        db.flush()
        result = "created"
    else:
        ticket = existing
        result = "updated"

    _apply_ticket_fields(ticket, ticket_payload, subscriber_id)
    db.flush()
    return {
        "result": result,
        "crm_ticket_id": str(ticket_payload.get("id") or ""),
        "local_ticket_id": str(ticket.id),
    }


def _find_ticket_by_crm_id(db: Session, crm_ticket_id: str) -> Ticket | None:
    if not crm_ticket_id:
        return None
    return (
        db.query(Ticket)
        .filter(Ticket.metadata_["crm_ticket_id"].as_string() == crm_ticket_id)
        .first()
    )


def _process_comment_created(
    db: Session, envelope: dict[str, Any]
) -> dict[str, str | None]:
    payload = _as_dict(envelope.get("payload"))
    context = _as_dict(envelope.get("context"))
    crm_ticket_id = _crm_ticket_id_from_payload("ticket.comment_created", envelope, payload)
    if not crm_ticket_id:
        return {"result": "ignored_missing_ticket_id", "crm_ticket_id": None}

    ticket = _find_ticket_by_crm_id(db, crm_ticket_id)
    if ticket is None and isinstance(payload.get("ticket"), dict):
        ticket_result = _upsert_ticket_from_webhook(db, "ticket.updated", envelope)
        if ticket_result.get("local_ticket_id"):
            ticket = db.get(Ticket, _coerce_uuid(str(ticket_result["local_ticket_id"])))
    if ticket is None:
        return {
            "result": "skipped_missing_local_ticket",
            "crm_ticket_id": crm_ticket_id,
        }

    crm_comment_id = _value(
        payload.get("id"),
        payload.get("comment_id"),
        context.get("comment_id"),
        envelope.get("event_id"),
    )
    if crm_comment_id and _comment_exists(db, crm_comment_id):
        return {
            "result": "comment_duplicate",
            "crm_ticket_id": crm_ticket_id,
            "crm_comment_id": crm_comment_id,
            "local_ticket_id": str(ticket.id),
        }

    body = _value(
        payload.get("body"),
        payload.get("comment"),
        payload.get("message"),
        payload.get("content"),
    ) or "(empty comment)"
    comment = TicketComment(
        ticket_id=ticket.id,
        author_person_id=None,
        author_type=TicketCommentAuthorType.system.value,
        body=body,
        is_internal=bool(payload.get("is_internal", False)),
        attachments=_clean_attachments(payload.get("attachments")),
        metadata_={
            "sync_source": "crm",
            "crm_comment_id": crm_comment_id,
            "crm_ticket_id": crm_ticket_id,
            "crm_author_person_id": _value(payload.get("author_person_id")),
        },
        created_at=_parse_datetime(payload.get("created_at"))
        or _parse_datetime(envelope.get("occurred_at"))
        or datetime.now(UTC),
    )
    db.add(comment)
    db.flush()
    return {
        "result": "comment_created",
        "crm_ticket_id": crm_ticket_id,
        "crm_comment_id": crm_comment_id,
        "local_ticket_id": str(ticket.id),
        "local_comment_id": str(comment.id),
    }


def _process_ticket_event(
    db: Session, event_type: str, envelope: dict[str, Any]
) -> dict[str, str | None]:
    if event_type == "ticket.comment_created":
        return _process_comment_created(db, envelope)
    return _upsert_ticket_from_webhook(db, event_type, envelope)


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
async def receive_crm_event(request: Request, db: Session = Depends(get_db)) -> dict:
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        payload = {}

    event_type = _event_type_from_request(request, payload)
    if event_type not in TICKET_EVENTS:
        # Acknowledge so the CRM doesn't retry events we don't consume.
        return {"status": "ignored", "event": event_type}

    delivery_id = _delivery_id_from_request(request)
    existing_delivery = db.get(CrmWebhookDelivery, delivery_id)
    if existing_delivery is not None:
        return {
            "status": "duplicate",
            "event": event_type,
            "delivery_id": str(delivery_id),
            "result": existing_delivery.result,
        }

    result = _process_ticket_event(db, event_type, payload)
    delivery = CrmWebhookDelivery(
        delivery_id=delivery_id,
        event_id=str(payload.get("event_id") or "") or None,
        event_type=event_type,
        crm_ticket_id=result.get("crm_ticket_id"),
        crm_comment_id=result.get("crm_comment_id"),
        status="processed",
        result=result.get("result"),
        payload=payload,
        processed_at=datetime.now(UTC),
    )
    db.add(delivery)
    db.commit()
    return {
        "status": "processed",
        "event": event_type,
        "delivery_id": str(delivery_id),
        **result,
    }


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
    _verify_signature(
        raw_body,
        request.headers.get(SIGNATURE_HEADER),
        secret=settings.crm_chat_webhook_secret,
    )

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

    # The CRM wraps event data under "payload" (event envelope); tolerate a flat
    # body too so the contract isn't brittle.
    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    subscriber_id = str(body.get("subscriber_id") or "").strip()
    if not subscriber_id:
        # Reseller-originated or unmapped chats have no device to wake; ack so
        # the CRM doesn't retry.
        return {"status": "ignored", "reason": "no_subscriber"}

    from app.services import push as push_service

    preview = str(body.get("preview") or "").strip() or "You have a new message."
    push_service.send_push(
        db,
        subscriber_id,
        title="New message from support",
        body=preview,
        data={
            "type": "chat_message",
            "conversation_id": str(body.get("conversation_id") or ""),
        },
    )
    return {"status": "ok", "event": event_type}


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

    inner = payload.get("payload")
    body = inner if isinstance(inner, dict) else payload

    return quotes_mirror.apply_webhook(db, event_type, body)
