from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.domain_settings import SettingDomain
from app.models.team_inbox import InboxChannelType, InboxMessage, InboxMessageDirection
from app.services import team_inbox_channel_receive
from app.services.credential_crypto import decrypt_credential
from app.services.settings_spec import resolve_value

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp-webhook"])

SIGNATURE_HEADER = "X-Hub-Signature-256"


def _secret_setting(db: Session, key: str) -> str:
    raw = str(resolve_value(db, SettingDomain.comms, key) or "").strip()
    if not raw:
        return ""
    decrypted = decrypt_credential(raw)
    return str(decrypted or raw).strip()


def _verify_token(db: Session) -> str:
    return _secret_setting(db, "meta_webhook_verify_token")


def _app_secret(db: Session) -> str:
    return _secret_setting(db, "meta_app_secret")


def _verify_meta_signature(db: Session, raw_body: bytes, presented: str | None) -> None:
    secret = _app_secret(db)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Meta webhook signature verification is not configured.",
        )
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Meta webhook signature.",
        )


def _text_body(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, dict):
        return str(text.get("body") or "").strip()
    return str(text or "").strip()


def _iter_meta_whatsapp_messages(payload: dict[str, Any]):
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_metadata = value.get("metadata")
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_contacts = value.get("contacts")
            contacts = raw_contacts if isinstance(raw_contacts, list) else []
            names_by_wa_id: dict[str, object] = {}
            for contact in contacts:
                if not isinstance(contact, dict) or not contact.get("wa_id"):
                    continue
                profile = contact.get("profile")
                names_by_wa_id[str(contact["wa_id"])] = (
                    profile.get("name") if isinstance(profile, dict) else None
                )
            raw_messages = value.get("messages")
            messages = raw_messages if isinstance(raw_messages, list) else []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                body = _text_body(message)
                sender = str(message.get("from") or "").strip()
                if not sender or not body:
                    continue
                yield {
                    "message": {
                        "from": sender,
                        "text": body,
                        "id": str(message.get("id") or "").strip() or None,
                    },
                    "contact_name": names_by_wa_id.get(sender),
                    "metadata": metadata,
                    "raw_message": message,
                }


def _iter_meta_whatsapp_statuses(payload: dict[str, Any]):
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            raw_statuses = value.get("statuses")
            statuses = raw_statuses if isinstance(raw_statuses, list) else []
            for status_item in statuses:
                if not isinstance(status_item, dict):
                    continue
                message_id = str(status_item.get("id") or "").strip()
                status_text = str(status_item.get("status") or "").strip()
                if not message_id or not status_text:
                    continue
                yield {
                    "message_id": message_id,
                    "status": status_text,
                    "timestamp": status_item.get("timestamp"),
                    "recipient_id": status_item.get("recipient_id"),
                    "errors": status_item.get("errors"),
                    "raw": status_item,
                }


def _apply_whatsapp_status(
    db: Session, status_item: dict[str, Any]
) -> dict[str, object]:
    provider_message_id = str(status_item["message_id"])
    message = (
        db.query(InboxMessage)
        .filter(InboxMessage.channel_type == InboxChannelType.whatsapp.value)
        .filter(InboxMessage.direction == InboxMessageDirection.outbound.value)
        .filter(InboxMessage.external_message_id == provider_message_id)
        .order_by(InboxMessage.created_at.desc())
        .first()
    )
    if message is None:
        return {
            "kind": "not_found",
            "provider_message_id": provider_message_id,
            "status": status_item["status"],
        }

    metadata = dict(message.metadata_ or {})
    history = metadata.get("delivery_status_history")
    if not isinstance(history, list):
        history = []
    event = {
        "status": status_item["status"],
        "timestamp": status_item.get("timestamp"),
        "recipient_id": status_item.get("recipient_id"),
        "errors": status_item.get("errors"),
    }
    history.append({key: value for key, value in event.items() if value is not None})
    metadata["delivery_status"] = status_item["status"]
    metadata["delivery_status_at"] = status_item.get("timestamp")
    metadata["delivery_recipient_id"] = status_item.get("recipient_id")
    if status_item.get("errors") is not None:
        metadata["delivery_errors"] = status_item["errors"]
    metadata["delivery_status_history"] = history[-20:]
    message.metadata_ = metadata
    return {
        "kind": "updated",
        "message_id": str(message.id),
        "provider_message_id": provider_message_id,
        "status": status_item["status"],
    }


@router.get("/meta")
def verify_meta_webhook(
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


@router.post("/meta")
async def receive_meta_whatsapp_webhook(
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
    status_results: list[dict[str, object]] = []
    for item in _iter_meta_whatsapp_messages(payload):
        inbound_payload = {
            "message": item["message"],
            "contact_name": item.get("contact_name"),
            "metadata": item.get("metadata"),
            "raw": item.get("raw_message"),
        }
        result = team_inbox_channel_receive.receive_whatsapp_webhook(
            db,
            provider="meta_cloud_api",
            payload=inbound_payload,
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
    for item in _iter_meta_whatsapp_statuses(payload):
        status_results.append(_apply_whatsapp_status(db, item))
    if results or status_results:
        db.commit()
    return {
        "status": "ok",
        "processed": len(results),
        "status_processed": len(status_results),
        "items": results,
        "status_items": status_results,
    }
