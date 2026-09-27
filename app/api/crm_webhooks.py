"""Verified DotMac CRM event ingress through the canonical Integration Inbox."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.integration_platform import IntegrationInbox
from app.services import quotes_mirror
from app.services.crm_customers import CRMCustomerObservation, observe_customer
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.crm_capability import inbound_secret_material

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/crm", tags=["crm-webhook"])

SIGNATURE_HEADER = "X-Webhook-Signature-256"
EVENT_HEADER = "X-Webhook-Event"
DELIVERY_HEADER = "X-Webhook-Delivery-Id"

CUSTOMER_EVENTS = {"customer.accepted"}
# `message.outbound` and the `/webhooks/crm/chat` receiver were REMOVED on
# 2026-08-30 with ADR 0006. They existed only to wake a mobile device when the
# CRM -- not Sub -- held the live-chat conversation. Sub's native Team Inbox is
# the sole live-chat authority again and pushes its own notifications, so an
# inbound CRM chat event has no consequence left to apply.
QUOTE_EVENTS = {
    "quote.created",
    "quote.updated",
    "quote.accepted",
    "quote.rejected",
}


def _verify_signature(raw_body: bytes, presented: str | None, secret: str) -> None:
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM webhook signature verification is not configured.",
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


async def _receive_verified(
    request: Request,
    db: Session,
    *,
    default_event: str,
) -> tuple[str, dict[str, Any], IntegrationInbox, bool]:
    try:
        binding, material = inbound_secret_material(db)
    except Exception as exc:
        logger.error("crm_inbound_capability_unavailable type=%s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CRM inbound integration is not enabled.",
        ) from exc

    raw_body = await request.body()
    _verify_signature(
        raw_body,
        request.headers.get(SIGNATURE_HEADER),
        str(material.get("webhook_signing_secret") or ""),
    )
    try:
        decoded = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from None
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )
    payload = dict(decoded)
    event_type = str(request.headers.get(EVENT_HEADER) or default_event).strip()
    provider_event_id = str(request.headers.get(DELIVERY_HEADER) or "").strip()
    if not provider_event_id:
        provider_event_id = f"{event_type}:{hashlib.sha256(raw_body).hexdigest()}"
    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db,
        capability_binding_id=binding.id,
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
        headers={
            key: value
            for key, value in {
                "content-type": request.headers.get("content-type"),
                "user-agent": request.headers.get("user-agent"),
            }.items()
            if value
        },
    )
    return event_type, payload, receipt, should_process


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else payload


def _complete(
    db: Session,
    receipt: IntegrationInbox,
    consequence: dict[str, Any],
) -> dict[str, Any]:
    return integration_inbox.complete_consequence(
        db,
        receipt=receipt,
        consequence=consequence,
    )


def _failed(
    db: Session,
    receipt: IntegrationInbox,
    exc: Exception,
    *,
    error_code: str = "crm_consequence_failed",
    error_detail: str | None = None,
) -> None:
    integration_inbox.fail_consequence(
        db,
        receipt=receipt,
        error_code=error_code,
        error_detail=error_detail or type(exc).__name__,
    )


def _existing(receipt: IntegrationInbox, should_process: bool) -> dict[str, Any] | None:
    if should_process:
        return None
    return dict(receipt.consequence_json or {})


@router.post("/customers")
async def receive_crm_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event="customer.accepted"
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in CUSTOMER_EVENTS:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        observation = CRMCustomerObservation.from_payload(payload)
        consequence = observe_customer(db, observation).as_consequence()
        return _complete(db, receipt, consequence)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY:
            integration_inbox.fail_consequence(
                db,
                receipt=receipt,
                error_code="crm_customer_name_rejected",
                error_detail="Customer name is missing or invalid.",
                consequence={
                    "status": "rejected",
                    "error_code": "crm_customer_name_rejected",
                    "name_disposition": "rejected",
                },
            )
        else:
            _failed(db, receipt, exc)
        raise
    except Exception as exc:
        _failed(db, receipt, exc)
        raise


async def _receive_mirror_event(
    request: Request,
    db: Session,
    *,
    allowed_events: set[str],
    default_event: str,
    consequence_owner: Callable[[Session, str, dict[str, Any]], dict[str, Any]],
    control_key: str | None = None,
) -> dict[str, Any]:
    event_type, payload, receipt, should_process = await _receive_verified(
        request, db, default_event=default_event
    )
    prior = _existing(receipt, should_process)
    if prior is not None:
        return prior
    try:
        if event_type not in allowed_events:
            return _complete(db, receipt, {"status": "ignored", "event": event_type})
        if control_key:
            from app.services import control_registry

            if not control_registry.is_enabled(db, control_key):
                return _complete(
                    db,
                    receipt,
                    {
                        "status": "ignored",
                        "reason": "observation_disabled",
                        "event": event_type,
                    },
                )
        consequence = consequence_owner(db, event_type, _body(payload))
        return _complete(db, receipt, consequence)
    except Exception as exc:
        _failed(db, receipt, exc)
        raise


@router.post("/quotes")
async def receive_crm_quote_event(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await _receive_mirror_event(
        request,
        db,
        allowed_events=QUOTE_EVENTS,
        default_event="quote.updated",
        consequence_owner=quotes_mirror.apply_webhook,
    )
