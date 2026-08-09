"""Meta social messaging transport for Team Inbox.

Supports two auth modes:
- ``oauth``: use stored Meta OAuth account tokens.
- ``override``: use per-channel operator supplied tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.models.oauth_token import OAuthToken
from app.models.team_inbox import InboxChannelType
from app.services import settings_spec
from app.services.secrets import resolve_secret


class MetaSocialAuthMode(StrEnum):
    oauth = "oauth"
    override = "override"


@dataclass(frozen=True)
class MetaSocialSendResult:
    provider_message_id: str | None
    recipient_id: str | None
    response: dict[str, Any]


@dataclass(frozen=True)
class MetaSocialAttachment:
    type: str
    url: str


@dataclass(frozen=True)
class MetaSocialProfile:
    display_name: str | None
    username: str | None
    profile_pic: str | None
    response: dict[str, Any]


def _setting_text(db: Session, key: str) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.comms, key)
    if not isinstance(value, str):
        return None
    resolved = resolve_secret(value)
    if not resolved:
        return None
    text = str(resolved).strip()
    return text or None


def auth_mode(db: Session) -> MetaSocialAuthMode:
    raw = _setting_text(db, "meta_social_auth_mode") or MetaSocialAuthMode.oauth.value
    try:
        return MetaSocialAuthMode(raw)
    except ValueError:
        return MetaSocialAuthMode.oauth


def graph_base_url(db: Session) -> str:
    version = (
        _setting_text(db, "meta_graph_api_version") or settings.meta_graph_api_version
    )
    return f"https://graph.facebook.com/{version}"


def instagram_graph_base_url(db: Session) -> str:
    version = (
        _setting_text(db, "meta_graph_api_version") or settings.meta_graph_api_version
    )
    return f"https://graph.instagram.com/{version}"


def webhook_verify_token(db: Session) -> str | None:
    return _setting_text(db, "meta_webhook_verify_token")


def webhook_signing_secret(db: Session) -> str | None:
    return _setting_text(db, "meta_app_secret")


def compute_webhook_signature(raw_body: bytes, app_secret: str) -> str:
    return (
        "sha256="
        + hmac.new(
            app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )


def verify_webhook_signature(
    raw_body: bytes,
    presented: str | None,
    app_secret: str,
) -> bool:
    expected = compute_webhook_signature(raw_body, app_secret)
    return bool(presented) and hmac.compare_digest(presented, expected)


def _token_record(
    db: Session,
    *,
    channel_type: str,
    account_id: str | None,
) -> OAuthToken | None:
    if channel_type == InboxChannelType.facebook_messenger.value:
        account_type = "page"
    elif channel_type == InboxChannelType.instagram_dm.value:
        account_type = "instagram_business"
    else:
        return None
    query = (
        db.query(OAuthToken)
        .filter(OAuthToken.provider == "meta")
        .filter(OAuthToken.account_type == account_type)
        .filter(OAuthToken.is_active.is_(True))
        .filter(OAuthToken.access_token.isnot(None))
    )
    if account_id:
        token = (
            query.filter(OAuthToken.external_account_id == account_id)
            .order_by(OAuthToken.created_at.desc())
            .first()
        )
        if token:
            return token
    return query.order_by(OAuthToken.created_at.desc()).first()


def _override_token(db: Session, channel_type: str) -> str | None:
    if channel_type == InboxChannelType.facebook_messenger.value:
        return _setting_text(db, "meta_facebook_access_token_override")
    if channel_type == InboxChannelType.instagram_dm.value:
        return _setting_text(db, "meta_instagram_access_token_override")
    return None


def _is_instagram_login_token(token: str | None) -> bool:
    return bool(token and token.strip().upper().startswith("IG"))


def _access_token(
    db: Session,
    *,
    channel_type: str,
    account_id: str | None,
) -> tuple[str, OAuthToken | None]:
    mode = auth_mode(db)
    token = _token_record(db, channel_type=channel_type, account_id=account_id)
    if mode is MetaSocialAuthMode.override:
        override = _override_token(db, channel_type)
        if override:
            return override, token
    if token and token.access_token:
        resolved = resolve_secret(token.access_token)
        if resolved:
            return resolved, token
    if mode is MetaSocialAuthMode.override:
        raise ValueError(f"No {channel_type} override token configured")
    raise ValueError(f"No active {channel_type} OAuth token configured")


def _message_payload(
    message_text: str, attachments: tuple[MetaSocialAttachment, ...] = ()
) -> dict[str, Any]:
    if attachments:
        attachment = attachments[0]
        return {
            "attachment": {
                "type": attachment.type,
                "payload": {"url": attachment.url, "is_reusable": True},
            }
        }
    return {"text": message_text}


def send_message(
    db: Session,
    *,
    channel_type: str,
    recipient_id: str,
    message_text: str,
    account_id: str | None,
    attachments: tuple[MetaSocialAttachment, ...] = (),
) -> MetaSocialSendResult:
    clean_recipient = recipient_id.strip()
    clean_body = message_text.strip()
    if not clean_recipient:
        raise ValueError("Meta social recipient is required")
    if not clean_body and not attachments:
        raise ValueError("Meta social message body is required")
    access_token, token = _access_token(
        db,
        channel_type=channel_type,
        account_id=account_id,
    )

    if channel_type == InboxChannelType.facebook_messenger.value:
        page_id = account_id or (token.external_account_id if token else None)
        if not page_id:
            raise ValueError("Facebook Page ID is required for Messenger replies")
        endpoint = f"{graph_base_url(db).rstrip('/')}/{page_id}/messages"
        params = {"access_token": access_token}
        headers = None
        payload = {
            "recipient": {"id": clean_recipient},
            "messaging_type": "RESPONSE",
            "message": _message_payload(clean_body, attachments),
        }
    elif channel_type == InboxChannelType.instagram_dm.value:
        if attachments:
            raise ValueError(
                "Instagram DM media replies require a public Meta media URL"
            )
        if _is_instagram_login_token(access_token):
            endpoint = f"{instagram_graph_base_url(db).rstrip('/')}/me/messages"
            params = None
            headers = {"Authorization": f"Bearer {access_token}"}
            payload = {
                "recipient": json.dumps({"id": clean_recipient}, separators=(",", ":")),
                "message": json.dumps(
                    _message_payload(clean_body), separators=(",", ":")
                ),
            }
        else:
            ig_account_id = account_id or (token.external_account_id if token else None)
            if not ig_account_id:
                raise ValueError("Instagram account ID is required for DM replies")
            endpoint = f"{graph_base_url(db).rstrip('/')}/{ig_account_id}/messages"
            params = {"access_token": access_token}
            headers = None
            payload = {
                "recipient": {"id": clean_recipient},
                "message": _message_payload(clean_body),
            }
    else:
        raise ValueError(f"Unsupported Meta social channel: {channel_type}")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(endpoint, params=params, headers=headers, json=payload)
    response.raise_for_status()
    raw_data = response.json()
    data = raw_data if isinstance(raw_data, dict) else {}
    return MetaSocialSendResult(
        provider_message_id=data.get("message_id"),
        recipient_id=data.get("recipient_id"),
        response=data,
    )


def fetch_contact_profile(
    db: Session,
    *,
    channel_type: str,
    contact_id: str,
    account_id: str | None,
) -> MetaSocialProfile | None:
    clean_contact_id = str(contact_id or "").strip()
    if not clean_contact_id:
        return None
    try:
        access_token, _token = _access_token(
            db, channel_type=channel_type, account_id=account_id
        )
    except Exception:
        return None
    if channel_type == InboxChannelType.facebook_messenger.value:
        endpoint = f"{graph_base_url(db).rstrip('/')}/{clean_contact_id}"
        fields = "first_name,last_name,name,profile_pic"
    elif channel_type == InboxChannelType.instagram_dm.value:
        endpoint = f"{graph_base_url(db).rstrip('/')}/{clean_contact_id}"
        fields = "username,name,profile_pic"
    else:
        return None
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(
                endpoint,
                params={"fields": fields, "access_token": access_token},
            )
        response.raise_for_status()
        raw_data = response.json()
    except Exception:
        return None
    data = raw_data if isinstance(raw_data, dict) else {}
    first = str(data.get("first_name") or "").strip()
    last = str(data.get("last_name") or "").strip()
    name = str(data.get("name") or "").strip()
    username = str(data.get("username") or "").strip()
    display_name = " ".join(part for part in (first, last) if part).strip()
    return MetaSocialProfile(
        display_name=display_name or name or username or None,
        username=username or None,
        profile_pic=str(data.get("profile_pic") or "").strip() or None,
        response=data,
    )
