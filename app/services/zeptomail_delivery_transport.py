"""ZeptoMail status lookup and signed-webhook transport helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value


class ZeptoMailDeliveryConfigurationError(RuntimeError):
    pass


class ZeptoMailWebhookVerificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ZeptoMailStatusFact:
    notification_id: UUID
    provider_status: str
    observed_at: datetime
    email_reference: str | None = None
    request_id: str | None = None
    reason: str | None = None


def _setting(db: Session, key: str) -> str:
    return str(resolve_value(db, SettingDomain.notification, key) or "").strip()


def delivery_tracking_enabled(db: Session) -> bool:
    return bool(
        resolve_value(
            db,
            SettingDomain.notification,
            "zeptomail_delivery_tracking_enabled",
        )
    )


def _parse_time(value: object, *, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (
                parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            )
        except ValueError:
            pass
    return fallback or datetime.now(UTC)


def _nested_reason(payload: dict[str, Any]) -> str | None:
    candidates: list[object] = [
        payload.get("reason"),
        payload.get("message"),
        payload.get("diagnostic_message"),
    ]
    event_message = payload.get("event_message")
    if isinstance(event_message, dict):
        event_data = event_message.get("event_data")
        if isinstance(event_data, dict):
            details = event_data.get("details")
            if isinstance(details, dict):
                candidates.extend(
                    [details.get("reason"), details.get("diagnostic_message")]
                )
    parts = [str(value).strip() for value in candidates if str(value or "").strip()]
    return " — ".join(dict.fromkeys(parts))[:1000] or None


class ZeptoMailLogClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        accounts_base_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.accounts_base_url = accounts_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None

    @classmethod
    def from_settings(cls, db: Session) -> ZeptoMailLogClient:
        values = {
            key: _setting(db, key)
            for key in (
                "zeptomail_api_base_url",
                "zeptomail_accounts_base_url",
                "zeptomail_oauth_client_id",
                "zeptomail_oauth_client_secret",
                "zeptomail_oauth_refresh_token",
            )
        }
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ZeptoMailDeliveryConfigurationError(
                "ZeptoMail status tracking settings are incomplete: "
                + ", ".join(missing)
            )
        return cls(
            api_base_url=values["zeptomail_api_base_url"],
            accounts_base_url=values["zeptomail_accounts_base_url"],
            client_id=values["zeptomail_oauth_client_id"],
            client_secret=values["zeptomail_oauth_client_secret"],
            refresh_token=values["zeptomail_oauth_refresh_token"],
        )

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        response = httpx.post(
            f"{self.accounts_base_url}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        token = str(response.json().get("access_token") or "").strip()
        if not token:
            raise ZeptoMailDeliveryConfigurationError(
                "ZeptoMail OAuth response did not contain an access token"
            )
        self._access_token = token
        return token

    def lookup(self, notification_id: UUID) -> ZeptoMailStatusFact | None:
        response = httpx.get(
            f"{self.api_base_url}/email",
            params={"client_reference": str(notification_id), "limit": 1},
            headers={"Authorization": f"Zoho-oauthtoken {self._token()}"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        raw_data = response.json().get("data")
        if not isinstance(raw_data, list) or not raw_data:
            return None
        item = raw_data[0]
        if not isinstance(item, dict) or not str(item.get("status") or "").strip():
            return None
        return ZeptoMailStatusFact(
            notification_id=notification_id,
            provider_status=str(item["status"]),
            observed_at=_parse_time(item.get("sent_time")),
            email_reference=str(item.get("email_reference") or "").strip() or None,
            request_id=str(item.get("request_id") or "").strip() or None,
            reason=_nested_reason(item),
        )


def _signature_parts(value: str) -> dict[str, str]:
    return {
        key.strip(): unquote(raw.strip())
        for part in value.split(";")
        if "=" in part
        for key, raw in [part.split("=", 1)]
    }


def parse_signed_webhook(
    *,
    raw_body: bytes,
    producer_signature: str | None,
    authentication_key: str,
    now: datetime | None = None,
    tolerance_seconds: int = 300,
) -> ZeptoMailStatusFact:
    if not producer_signature or not authentication_key:
        raise ZeptoMailWebhookVerificationError("Missing webhook authentication")
    signature = _signature_parts(producer_signature)
    if signature.get("s-algorithm") != "HmacSHA256":
        raise ZeptoMailWebhookVerificationError("Unsupported webhook signature")
    try:
        signed_at = datetime.fromtimestamp(int(signature["ts"]) / 1000, tz=UTC)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ZeptoMailWebhookVerificationError("Invalid webhook timestamp") from exc
    current = now or datetime.now(UTC)
    if abs((current - signed_at).total_seconds()) > tolerance_seconds:
        raise ZeptoMailWebhookVerificationError("Expired webhook signature")
    form = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    values = form.get("eventData") or form.get("event_data")
    if not values:
        raise ZeptoMailWebhookVerificationError("Missing webhook event data")
    event_json = values[0]
    expected = hmac.new(
        authentication_key.encode("utf-8"),
        event_json.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    try:
        received = base64.b64decode(signature["s"], validate=True)
    except (KeyError, ValueError) as exc:
        raise ZeptoMailWebhookVerificationError("Invalid webhook signature") from exc
    if not hmac.compare_digest(received, expected):
        raise ZeptoMailWebhookVerificationError("Invalid webhook signature")
    try:
        payload = json.loads(event_json)
    except json.JSONDecodeError as exc:
        raise ZeptoMailWebhookVerificationError("Invalid webhook payload") from exc
    if not isinstance(payload, dict):
        raise ZeptoMailWebhookVerificationError("Invalid webhook payload")
    event_message = payload.get("event_message")
    if not isinstance(event_message, dict):
        event_message = {}
    email_info = event_message.get("email_info")
    if not isinstance(email_info, dict):
        email_info = {}
    client_reference = str(email_info.get("client_reference") or "").strip()
    try:
        notification_id = UUID(client_reference)
    except ValueError as exc:
        raise ZeptoMailWebhookVerificationError(
            "Webhook client reference is not recognized"
        ) from exc
    event_name = str(payload.get("event_name") or "").strip()
    event_data = event_message.get("event_data")
    details = event_data.get("details") if isinstance(event_data, dict) else None
    observed_value = details.get("time") if isinstance(details, dict) else None
    return ZeptoMailStatusFact(
        notification_id=notification_id,
        provider_status=event_name,
        observed_at=_parse_time(
            observed_value or email_info.get("processed_time"), fallback=signed_at
        ),
        email_reference=str(email_info.get("email_reference") or "").strip() or None,
        request_id=str(event_message.get("request_id") or "").strip() or None,
        reason=_nested_reason(payload),
    )


def webhook_authentication_key(db: Session) -> str:
    return _setting(db, "zeptomail_webhook_authentication_key")
