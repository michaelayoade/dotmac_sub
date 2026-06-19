"""WhatsApp connector helper service.

Provides provider-agnostic helpers for message send payloads and webhook
normalization. Network calls are optional and disabled by default.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services.credential_crypto import decrypt_credential
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

WHATSAPP_PROVIDER_META = "meta_cloud_api"
WHATSAPP_PROVIDER_TWILIO = "twilio"
WHATSAPP_PROVIDER_MESSAGEBIRD = "messagebird"
SUPPORTED_WHATSAPP_PROVIDERS = {
    WHATSAPP_PROVIDER_META,
    WHATSAPP_PROVIDER_TWILIO,
    WHATSAPP_PROVIDER_MESSAGEBIRD,
}
_TEMPLATE_VARIABLE_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


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
        "waba_id": _read_setting(db, "whatsapp_waba_id"),
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


def _meta_messages_endpoint(db: Session, config: dict[str, Any]) -> str:
    graph_version = str(
        resolve_value(db, SettingDomain.comms, "meta_graph_api_version") or "v21.0"
    ).strip()
    if not graph_version:
        graph_version = "v21.0"
    if not graph_version.startswith("v"):
        graph_version = f"v{graph_version}"
    phone_number_id = str(config.get("phone_number") or "").strip()
    return f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"


def _meta_api_base(db: Session) -> str:
    graph_version = str(
        resolve_value(db, SettingDomain.comms, "meta_graph_api_version") or "v21.0"
    ).strip()
    if not graph_version:
        graph_version = "v21.0"
    if not graph_version.startswith("v"):
        graph_version = f"v{graph_version}"
    return f"https://graph.facebook.com/{graph_version}"


def _template_language(config: dict[str, Any], template_name: str) -> str:
    for template in config.get("templates") or []:
        if str(template.get("name") or "") == template_name:
            language = str(template.get("language") or "").strip()
            if language:
                return language
    return "en"


def _ordered_template_parameters(variables: dict[str, Any] | None) -> list[str]:
    if not variables:
        return []
    ordered: list[tuple[int, str]] = []
    trailing: list[str] = []
    for key, value in variables.items():
        key_text = str(key).strip()
        value_text = "" if value is None else str(value)
        if key_text.isdigit():
            ordered.append((int(key_text), value_text))
        else:
            trailing.append(value_text)
    ordered_values = [
        value for _index, value in sorted(ordered, key=lambda item: item[0])
    ]
    return ordered_values + trailing


def _fallback_example(index: int) -> str:
    examples = {
        1: "Customer Name",
        2: "Account Number",
        3: "Date",
        4: "Amount",
    }
    return examples.get(index, "Value")


def extract_template_variables(
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return BODY placeholder metadata for ``{{1}}`` style template variables."""
    body_text = ""
    for component in components:
        if str(component.get("type") or "").upper() == "BODY":
            body_text = str(component.get("text") or "")
            break
    seen: set[int] = set()
    variables: list[dict[str, Any]] = []
    for match in _TEMPLATE_VARIABLE_RE.finditer(body_text):
        index = int(match.group(1))
        if index in seen:
            continue
        seen.add(index)
        label = f"Field {index}"
        example = _fallback_example(index)
        variables.append(
            {
                "index": index,
                "key": str(index),
                "label": label,
                "example": example,
                "placeholder": f"Select a source for {label.lower()}",
            }
        )
    return variables


def fetch_template_details(
    db: Session, *, template_name: str, language: str | None = None
) -> dict[str, Any]:
    """Fetch one WhatsApp template's full component details from Meta."""
    config = load_whatsapp_config(db)
    _require_config(config)
    if str(config.get("provider") or "") != WHATSAPP_PROVIDER_META:
        raise WhatsAppConfigError("Template detail lookup is only supported for Meta")
    waba_id = str(config.get("waba_id") or "").strip()
    if not waba_id:
        raise WhatsAppConfigError("WhatsApp Business Account ID is required")

    response = httpx.get(
        f"{_meta_api_base(db)}/{waba_id}/message_templates",
        params={
            "fields": "name,status,language,category,components",
            "name": template_name,
            "limit": 100,
        },
        headers={"Authorization": f"Bearer {config['api_key']}"},
        timeout=int(
            resolve_value(db, SettingDomain.comms, "whatsapp_api_timeout_seconds")
            or 10
        ),
    )
    if response.status_code >= 400:
        raise WhatsAppConfigError(response.text)
    templates = response.json().get("data") or []
    language_filter = (language or "").strip()
    for template in templates:
        if str(template.get("name") or "") != template_name:
            continue
        if language_filter and str(template.get("language") or "") != language_filter:
            continue
        components = template.get("components") or []
        return {
            "name": template.get("name"),
            "status": template.get("status"),
            "language": template.get("language"),
            "category": template.get("category"),
            "components": components,
            "variables": extract_template_variables(components),
        }
    raise WhatsAppConfigError("WhatsApp template not found")


def build_template_payload(
    *,
    provider: str,
    recipient: str,
    template_name: str,
    language: str = "en",
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build provider-specific WhatsApp template payload without sending."""
    safe_vars = variables or {}
    if provider == WHATSAPP_PROVIDER_META:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language or "en"},
            },
        }
        params = _ordered_template_parameters(safe_vars)
        if params:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": value,
                        }
                        for value in params
                    ],
                }
            ]
        return payload
    if provider == WHATSAPP_PROVIDER_TWILIO:
        return {
            "To": f"whatsapp:{recipient}",
            "ContentSid": template_name,
            "ContentVariables": json.dumps(
                {key: str(value) for key, value in safe_vars.items()}
            ),
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
    language: str | None = None,
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
        language=language or _template_language(config, template_name),
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
    timeout_seconds = int(
        resolve_value(db, SettingDomain.comms, "whatsapp_api_timeout_seconds") or 10
    )
    headers = {"Content-Type": "application/json"}
    endpoint = ""

    if provider == WHATSAPP_PROVIDER_META:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = _meta_messages_endpoint(db, config)
    elif provider == WHATSAPP_PROVIDER_TWILIO:
        headers["Authorization"] = f"Bearer {config['api_key']}"
        endpoint = "https://api.twilio.com/2010-04-01/Accounts/Messages.json"
    elif provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        headers["Authorization"] = f"AccessKey {config['api_key']}"
        endpoint = "https://conversations.messagebird.com/v1/send"

    response = httpx.post(
        endpoint, json=payload, headers=headers, timeout=timeout_seconds
    )
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
        endpoint = _meta_messages_endpoint(db, config)
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

    timeout_seconds = int(
        resolve_value(db, SettingDomain.comms, "whatsapp_api_timeout_seconds") or 10
    )
    response = httpx.post(
        endpoint, json=payload, headers=headers, timeout=timeout_seconds
    )
    return {
        "ok": response.status_code < 400,
        "provider": provider,
        "sent": True,
        "status_code": response.status_code,
        "response": response.text,
    }


def normalize_inbound_webhook(
    *, provider: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Normalize inbound provider webhook payload to a common shape."""
    if provider == WHATSAPP_PROVIDER_META:
        message = payload.get("message") or {}
        sender = message.get("from") or payload.get("from")
        return {
            "provider": provider,
            "from": sender,
            "normalized_from": normalize_phone_identifier(sender),
            "text": message.get("text") or payload.get("text"),
            "external_id": message.get("id") or payload.get("id"),
            "raw": payload,
        }
    if provider == WHATSAPP_PROVIDER_TWILIO:
        sender = payload.get("From")
        return {
            "provider": provider,
            "from": sender,
            "normalized_from": normalize_phone_identifier(sender),
            "text": payload.get("Body"),
            "external_id": payload.get("MessageSid"),
            "raw": payload,
        }
    if provider == WHATSAPP_PROVIDER_MESSAGEBIRD:
        sender = payload.get("from")
        return {
            "provider": provider,
            "from": sender,
            "normalized_from": normalize_phone_identifier(sender),
            "text": payload.get("text"),
            "external_id": payload.get("id"),
            "raw": payload,
        }
    raise WhatsAppConfigError("Unsupported WhatsApp provider")
