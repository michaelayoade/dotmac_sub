"""WhatsApp connector helper service.

Provides provider-agnostic helpers for message send payloads and webhook
normalization. Network calls are optional and disabled by default.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.credential_crypto import decrypt_credential
from app.services.settings_spec import resolve_value

WHATSAPP_PROVIDER_META = "meta_cloud_api"
WHATSAPP_PROVIDER_TWILIO = "twilio"
WHATSAPP_PROVIDER_MESSAGEBIRD = "messagebird"
SUPPORTED_WHATSAPP_PROVIDERS = {
    WHATSAPP_PROVIDER_META,
    WHATSAPP_PROVIDER_TWILIO,
    WHATSAPP_PROVIDER_MESSAGEBIRD,
}


class WhatsAppConfigError(ValueError):
    """Raised when WhatsApp configuration is invalid or missing."""



def _read_setting(db: Session, key: str, default: str = "") -> str:
    value = resolve_value(db, SettingDomain.comms, key)
    if value is None:
        return default
    return str(value).strip()



def _read_templates(db: Session) -> list[dict[str, Any]]:
    raw = resolve_value(db, SettingDomain.comms, "whatsapp_message_templates")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    return []



def load_whatsapp_config(db: Session) -> dict[str, Any]:
    """Load normalized WhatsApp connector settings."""
    provider = _read_setting(db, "whatsapp_provider", WHATSAPP_PROVIDER_META)
    if provider not in SUPPORTED_WHATSAPP_PROVIDERS:
        provider = WHATSAPP_PROVIDER_META

    api_key = _read_setting(db, "whatsapp_api_key")
    api_secret = _read_setting(db, "whatsapp_api_secret")

    return {
        "provider": provider,
        "phone_number": _read_setting(db, "whatsapp_phone_number"),
        "webhook_url": _read_setting(db, "whatsapp_webhook_url"),
        "api_key": decrypt_credential(api_key) if api_key else "",
        "api_secret": decrypt_credential(api_secret) if api_secret else "",
        "templates": _read_templates(db),
    }



def _require_config(config: dict[str, Any]) -> None:
    provider = str(config.get("provider", ""))
    if provider not in SUPPORTED_WHATSAPP_PROVIDERS:
        raise WhatsAppConfigError("Unsupported WhatsApp provider")
    if not str(config.get("api_key", "")).strip():
        raise WhatsAppConfigError("WhatsApp API key is required")
    if not str(config.get("phone_number", "")).strip():
        raise WhatsAppConfigError("WhatsApp phone number is required")



def build_template_payload(
    *,
    provider: str,
    recipient: str,
    template_name: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build provider-specific WhatsApp template payload without sending."""
    safe_vars = variables or {}
    if provider == WHATSAPP_PROVIDER_META:
        return {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(value)} for value in safe_vars.values()
                        ],
                    }
                ],
            },
        }
    if provider == WHATSAPP_PROVIDER_TWILIO:
        return {
            "To": f"whatsapp:{recipient}",
            "ContentSid": template_name,
            "ContentVariables": json.dumps({key: str(value) for key, value in safe_vars.items()}),
        }
    if provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        return {
            "to": recipient,
            "type": "hsm",
            "content": {
                "hsm": {
                    "namespace": "default",
                    "templateName": template_name,
                    "params": [str(value) for value in safe_vars.values()],
                }
            },
        }
    raise WhatsAppConfigError("Unsupported WhatsApp provider")



def send_template_message(
    db: Session,
    *,
    recipient: str,
    template_name: str,
    variables: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Send or preview a WhatsApp template message.

    Returns a normalized result that can be logged or shown in UI.
    """
    config = load_whatsapp_config(db)
    _require_config(config)
    payload = build_template_payload(
        provider=str(config["provider"]),
        recipient=recipient,
        template_name=template_name,
        variables=variables,
    )
    if dry_run:
        return {
            "ok": True,
            "provider": config["provider"],
            "sent": False,
            "payload": payload,
            "message": "Dry-run successful. Payload validated.",
        }

    provider = str(config["provider"])
    timeout_seconds = int(resolve_value(db, SettingDomain.comms, "whatsapp_api_timeout_seconds") or 10)
    headers = {"Content-Type": "application/json"}
    endpoint = ""

    if provider == WHATSAPP_PROVIDER_META:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = "https://graph.facebook.com/v21.0/messages"
    elif provider == WHATSAPP_PROVIDER_TWILIO:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = "https://api.twilio.com/2010-04-01/Accounts/Messages.json"
    elif provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        headers["Authorization"] = f"AccessKey {config['api_key']}"
        endpoint = "https://conversations.messagebird.com/v1/send"

    response = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
    return {
        "ok": response.status_code < 400,
        "provider": provider,
        "sent": True,
        "status_code": response.status_code,
        "response": response.text,
    }



def send_text_message(
    db: Session,
    *,
    recipient: str,
    body: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send or preview a plain WhatsApp text message."""
    config = load_whatsapp_config(db)
    _require_config(config)
    provider = str(config["provider"])
    headers: dict[str, str] = {"Content-Type": "application/json"}
    endpoint = ""
    payload: dict[str, Any]

    if provider == WHATSAPP_PROVIDER_META:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = "https://graph.facebook.com/v21.0/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient.strip(),
            "type": "text",
            "text": {"body": body},
        }
    elif provider == WHATSAPP_PROVIDER_TWILIO:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = "https://api.twilio.com/2010-04-01/Accounts/Messages.json"
        payload = {
            "To": f"whatsapp:{recipient.strip()}",
            "Body": body,
        }
    elif provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        headers["Authorization"] = f"AccessKey {config['api_key']}"
        endpoint = "https://conversations.messagebird.com/v1/send"
        payload = {
            "to": recipient.strip(),
            "type": "text",
            "content": {"text": body},
        }
    else:
        raise WhatsAppConfigError("Unsupported WhatsApp provider")

    if dry_run:
        return {
            "ok": True,
            "provider": provider,
            "sent": False,
            "payload": payload,
            "message": "Dry-run successful. Payload validated.",
        }

    timeout_seconds = int(resolve_value(db, SettingDomain.comms, "whatsapp_api_timeout_seconds") or 10)
    response = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout_seconds)
    return {
        "ok": response.status_code < 400,
        "provider": provider,
        "sent": True,
        "status_code": response.status_code,
        "response": response.text,
    }


def normalize_inbound_webhook(*, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize inbound provider webhook payload to a common shape."""
    if provider == WHATSAPP_PROVIDER_META:
        message = payload.get("message") or {}
        return {
            "provider": provider,
            "from": message.get("from") or payload.get("from"),
            "text": message.get("text") or payload.get("text"),
            "external_id": message.get("id") or payload.get("id"),
            "raw": payload,
        }
    if provider == WHATSAPP_PROVIDER_TWILIO:
        return {
            "provider": provider,
            "from": payload.get("From"),
            "text": payload.get("Body"),
            "external_id": payload.get("MessageSid"),
            "raw": payload,
        }
    if provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        return {
            "provider": provider,
            "from": payload.get("from"),
            "text": payload.get("text"),
            "external_id": payload.get("id"),
            "raw": payload,
        }
    raise WhatsAppConfigError("Unsupported WhatsApp provider")
