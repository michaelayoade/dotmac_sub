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
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.subscriber import Subscriber
from app.schemas.subscriber import SubscriberCreate
from app.services import subscriber as subscriber_service

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


def _clean_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _fallback_email(payload: dict, crm_person_id: str) -> str:
    email = _clean_text(payload.get("email"))
    if email:
        return email.lower()
    return f"crm-{crm_person_id}@selfcare.dotmac.io"


def _name_parts(payload: dict) -> tuple[str, str]:
    first_name = _clean_text(payload.get("first_name"))
    last_name = _clean_text(payload.get("last_name"))
    if first_name and last_name:
        return first_name, last_name

    display_name = _clean_text(payload.get("display_name"))
    if display_name:
        parts = display_name.split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return parts[0], "Customer"
    return first_name or "Customer", last_name or "Customer"


def _find_existing_customer(
    db: Session, *, crm_person_id: str, email: str
) -> Subscriber | None:
    existing = (
        db.query(Subscriber)
        .filter(
            func.lower(
                func.coalesce(Subscriber.metadata_["crm_person_id"].as_string(), "")
            )
            == crm_person_id.lower()
        )
        .first()
    )
    if existing:
        return existing
    return (
        db.query(Subscriber)
        .filter(func.lower(Subscriber.email) == email.lower())
        .first()
    )


def _subscriber_payload(payload: dict, *, crm_person_id: str) -> SubscriberCreate:
    first_name, last_name = _name_parts(payload)
    email = _fallback_email(payload, crm_person_id)
    metadata = (
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    )
    metadata = {
        **metadata,
        "source": "dotmac_omni",
        "crm_person_id": crm_person_id,
        "crm_project_id": _clean_text(payload.get("crm_project_id")),
        "crm_quote_id": _clean_text(payload.get("crm_quote_id")),
        "crm_sales_order_id": _clean_text(payload.get("crm_sales_order_id")),
    }
    return SubscriberCreate(
        first_name=first_name,
        last_name=last_name,
        display_name=_clean_text(payload.get("display_name")),
        email=email,
        phone=_clean_text(payload.get("phone")),
        address_line1=_clean_text(payload.get("address_line1")),
        address_line2=_clean_text(payload.get("address_line2")),
        city=_clean_text(payload.get("city")),
        region=_clean_text(payload.get("region")),
        postal_code=_clean_text(payload.get("postal_code")),
        country_code=_clean_text(payload.get("country_code")),
        status="new",
        billing_enabled=True,
        metadata_=metadata,
    )


@router.post("/customers")
async def receive_crm_customer(request: Request, db: Session = Depends(get_db)) -> dict:
    raw_body = await request.body()
    original_secret = settings.crm_webhook_secret
    customer_secret = settings.crm_customer_webhook_secret or original_secret
    object.__setattr__(settings, "crm_webhook_secret", customer_secret)
    try:
        _verify_signature(raw_body, request.headers.get(SIGNATURE_HEADER))
    finally:
        object.__setattr__(settings, "crm_webhook_secret", original_secret)

    event_type = str(request.headers.get(EVENT_HEADER) or "").strip()
    if event_type != "customer.accepted":
        return {"status": "ignored", "event": event_type}

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload."
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer payload."
        )

    crm_person_id = _clean_text(payload.get("crm_person_id"))
    if not crm_person_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="crm_person_id is required."
        )
    try:
        uuid.UUID(crm_person_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="crm_person_id must be a UUID.",
        ) from exc

    email = _fallback_email(payload, crm_person_id)
    existing = _find_existing_customer(db, crm_person_id=crm_person_id, email=email)
    if existing:
        metadata = dict(existing.metadata_ or {})
        metadata.update(
            _subscriber_payload(payload, crm_person_id=crm_person_id).metadata_ or {}
        )
        existing.metadata_ = metadata
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return {
            "status": "existing",
            "subscriber_id": str(existing.id),
            "subscriber_number": existing.subscriber_number,
        }

    try:
        subscriber_payload = _subscriber_payload(payload, crm_person_id=crm_person_id)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()
        ) from exc
    subscriber = subscriber_service.subscribers.create(db, subscriber_payload)
    return {
        "status": "created",
        "subscriber_id": str(subscriber.id),
        "subscriber_number": subscriber.subscriber_number,
    }
