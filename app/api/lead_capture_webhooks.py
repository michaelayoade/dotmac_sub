"""Signed, provider-neutral lead ingress through the Integration Inbox."""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.sales import LeadCaptureRead, LeadCaptureRequest
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.connectors.lead_capture_http import (
    LEAD_CAPTURE_CAPABILITY,
)
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
)
from app.services.sales import capture

router = APIRouter(prefix="/webhooks/lead-capture", tags=["lead-capture-webhook"])


@router.post("/{capability_binding_id}", response_model=LeadCaptureRead)
async def receive_lead_capture(
    capability_binding_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        context = build_execution_context(
            db, capability_binding_id=capability_binding_id
        )
    except RuntimeExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Lead-capture integration is unavailable",
        ) from exc
    if context.binding.capability_id != LEAD_CAPTURE_CAPABILITY:
        raise HTTPException(status_code=404, detail="Lead-capture binding not found")

    raw_body = await request.body()
    signature_header = str(context.config["signature_header"])
    delivery_header = str(context.config["delivery_id_header"])
    signature_prefix = str(context.config["signature_prefix"])
    secret = context.secret_material["webhook_signing_secret"]
    expected = (
        signature_prefix
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    )
    presented = request.headers.get(signature_header)
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    provider_event_id = str(request.headers.get(delivery_header) or "").strip()
    if not provider_event_id:
        raise HTTPException(status_code=400, detail="Webhook delivery id is required")
    try:
        decoded = await request.json()
        payload = LeadCaptureRequest.model_validate(decoded)
    except (ValueError, ValidationError):
        raise HTTPException(
            status_code=422, detail="Invalid lead-capture payload"
        ) from None

    try:
        receipt, _should_process = integration_inbox.receive_and_claim_verified(
            db,
            capability_binding_id=context.binding.id,
            provider_event_id=provider_event_id,
            event_type=LEAD_CAPTURE_CAPABILITY,
            payload=payload.model_dump(mode="json", exclude_none=True),
            headers={"content-type": str(request.headers.get("content-type") or "")},
        )
    except integration_inbox.ProviderEventIdentityCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "lead_receipt_conflict", "message": str(exc)},
        ) from exc
    except integration_inbox.InboxError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "lead_receipt_conflict", "message": str(exc)},
        ) from exc
    try:
        result = capture.capture_verified_receipt(
            db,
            receipt_id=receipt.id,
            payload=payload,
            actor_id=f"integration:{context.binding.installation_id}",
        )
    except capture.LeadCaptureError as exc:
        integration_inbox.fail_consequence(
            db,
            receipt=receipt,
            error_code=exc.code,
            error_detail=str(exc),
        )
        raise HTTPException(
            status_code=422 if exc.kind == "invalid" else 409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return LeadCaptureRead(
        lead_id=result.lead.id,
        party_id=result.party_id,
        origin_capture_id=result.origin.id,
        replayed=result.replayed,
    )
