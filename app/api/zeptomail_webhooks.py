"""Authenticated ZeptoMail delivery webhook adapter."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import zeptomail_delivery_reconciliation as reconciliation
from app.services import zeptomail_delivery_transport as transport
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext

router = APIRouter(prefix="/webhooks/zeptomail", tags=["zeptomail-webhook"])


@router.post("/delivery")
async def zeptomail_delivery_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    raw_body = await request.body()
    authentication_key = transport.webhook_authentication_key(db)
    db_session_adapter.release_read_transaction(db)
    try:
        fact = transport.parse_signed_webhook(
            raw_body=raw_body,
            producer_signature=request.headers.get("producer-signature"),
            authentication_key=authentication_key,
        )
    except transport.ZeptoMailWebhookVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc
    outcome = reconciliation.apply_zeptomail_delivery_status(
        db,
        reconciliation.ApplyZeptoMailDeliveryStatusCommand(
            context=CommandContext.system(
                actor="provider:zeptomail",
                scope="notifications:delivery-webhook",
                reason="Apply authenticated ZeptoMail delivery callback",
                idempotency_key=(
                    f"zeptomail:{fact.request_id or fact.email_reference or fact.notification_id}:"
                    f"{fact.provider_status}"
                ),
            ),
            notification_id=fact.notification_id,
            provider_status=fact.provider_status,
            observed_at=fact.observed_at,
            email_reference=fact.email_reference,
            request_id=fact.request_id,
            reason=fact.reason,
        ),
    )
    return {
        "result": outcome.kind,
        "status": outcome.status.value if outcome.status else "unknown",
    }
