from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.inbox_webhooks import (
    SIGNATURE_HEADER,
    _verify_meta_signature,
    _verify_token,
)
from app.db import get_db
from app.models.team_inbox import InboxChannelType
from app.services import team_inbox_channel_receive

router = APIRouter(prefix="/webhooks/meta", tags=["meta-inbox-webhook"])


def _event_timestamp(value: object) -> datetime | None:
    try:
        timestamp = int(str(value))
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC)


def _message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or "").strip()


def _channel_for_object(payload_object: object) -> str | None:
    object_name = str(payload_object or "").strip().lower()
    if object_name == "page":
        return InboxChannelType.facebook_messenger.value
    if object_name == "instagram":
        return InboxChannelType.instagram_dm.value
    return None


def _iter_meta_social_messages(payload: dict[str, Any]):
    channel_type = _channel_for_object(payload.get("object"))
    if not channel_type:
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        page_or_account_id = str(entry.get("id") or "").strip() or None
        for event in entry.get("messaging") or []:
            if not isinstance(event, dict):
                continue
            sender = event.get("sender")
            sender_id = (
                str(sender.get("id") or "").strip() if isinstance(sender, dict) else ""
            )
            message = event.get("message")
            if not sender_id or not isinstance(message, dict):
                continue
            body = _message_text(message)
            if not body:
                continue
            external_message_id = str(message.get("mid") or "").strip() or None
            yield {
                "channel_type": channel_type,
                "sender_id": sender_id,
                "body": body,
                "external_message_id": external_message_id,
                "received_at": _event_timestamp(event.get("timestamp")),
                "metadata": {
                    "provider": "meta",
                    "platform": channel_type,
                    "page_or_account_id": page_or_account_id,
                    "raw": event,
                },
            }


@router.get("")
def verify_meta_inbox_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
    db: Session = Depends(get_db),
):
    expected = _verify_token(db)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook verify token is not configured.",
        )
    if mode != "subscribe" or not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return PlainTextResponse(challenge or "")


@router.post("")
async def receive_meta_inbox_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    raw_body = await request.body()
    _verify_meta_signature(db, raw_body, request.headers.get(SIGNATURE_HEADER))
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )

    results: list[dict[str, object]] = []
    for item in _iter_meta_social_messages(payload):
        result = team_inbox_channel_receive.receive_inbound_channel(
            db,
            team_inbox_channel_receive.InboundChannelPayload(
                channel_type=str(item["channel_type"]),
                contact_address=str(item["sender_id"]),
                body=str(item["body"]),
                external_message_id=item.get("external_message_id"),
                received_at=item.get("received_at"),
                metadata=item.get("metadata"),
            ),
        )
        results.append(
            {
                "kind": result.kind,
                "conversation_id": result.conversation_id,
                "message_id": result.message_id,
                "resolution_status": result.resolution_status,
                "subscriber_id": result.subscriber_id,
                "reseller_id": result.reseller_id,
            }
        )
    if results:
        db.commit()
    return {
        "status": "ok",
        "processed": len(results),
        "items": results,
    }
