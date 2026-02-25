"""Web helpers for WhatsApp integration configuration page."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.credential_crypto import encrypt_credential
from app.services.integrations.connectors import whatsapp as whatsapp_connector
from app.services.settings_spec import resolve_value

_PROVIDER_OPTIONS = [
    {"id": whatsapp_connector.WHATSAPP_PROVIDER_META, "label": "Meta Cloud API"},
    {"id": whatsapp_connector.WHATSAPP_PROVIDER_TWILIO, "label": "Twilio"},
    {"id": whatsapp_connector.WHATSAPP_PROVIDER_MESSAGEBIRD, "label": "MessageBird"},
]


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * (len(value) - 4)}{value[-4:]}"



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



def build_config_state(db: Session) -> dict[str, Any]:
    config = whatsapp_connector.load_whatsapp_config(db)
    return {
        "provider_options": _PROVIDER_OPTIONS,
        "form": {
            "provider": config["provider"],
            "phone_number": config.get("phone_number", ""),
            "webhook_url": config.get("webhook_url", ""),
            "api_key": "",
            "api_secret": "",
            "api_key_masked": _mask_secret(str(config.get("api_key", ""))),
            "api_secret_masked": _mask_secret(str(config.get("api_secret", ""))),
            "message_templates_json": json.dumps(config.get("templates", []), indent=2),
        },
    }



def save_config(
    db: Session,
    *,
    provider: str,
    phone_number: str,
    webhook_url: str,
    api_key: str,
    api_secret: str,
    message_templates_json: str,
) -> None:
    provider_value = (provider or "").strip().lower()
    if provider_value not in {opt["id"] for opt in _PROVIDER_OPTIONS}:
        raise ValueError("Unsupported WhatsApp API provider")

    templates_value: list[dict[str, Any]] = []
    if (message_templates_json or "").strip():
        try:
            parsed = json.loads(message_templates_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Message templates must be valid JSON") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
            raise ValueError("Message templates must be a JSON array of objects")
        templates_value = parsed

    settings = domain_settings_service.comms_settings

    existing_encrypted_key = str(resolve_value(db, SettingDomain.comms, "whatsapp_api_key") or "")
    existing_encrypted_secret = str(resolve_value(db, SettingDomain.comms, "whatsapp_api_secret") or "")

    encrypted_key = encrypt_credential(api_key.strip()) if api_key.strip() else existing_encrypted_key
    encrypted_secret = (
        encrypt_credential(api_secret.strip()) if api_secret.strip() else existing_encrypted_secret
    )

    settings.upsert_by_key(
        db,
        "whatsapp_provider",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=provider_value),
    )
    settings.upsert_by_key(
        db,
        "whatsapp_phone_number",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=phone_number.strip()),
    )
    settings.upsert_by_key(
        db,
        "whatsapp_webhook_url",
        DomainSettingUpdate(value_type=SettingValueType.string, value_text=webhook_url.strip()),
    )
    settings.upsert_by_key(
        db,
        "whatsapp_api_key",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=encrypted_key,
            is_secret=True,
        ),
    )
    settings.upsert_by_key(
        db,
        "whatsapp_api_secret",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=encrypted_secret,
            is_secret=True,
        ),
    )
    settings.upsert_by_key(
        db,
        "whatsapp_message_templates",
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=templates_value,
            value_text=None,
        ),
    )



def run_test_send(
    db: Session,
    *,
    recipient: str,
    template_name: str,
    variables_json: str,
) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    if (variables_json or "").strip():
        try:
            parsed = json.loads(variables_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Variables must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Variables must be a JSON object")
        variables = parsed
    return whatsapp_connector.send_template_message(
        db,
        recipient=recipient.strip(),
        template_name=template_name.strip(),
        variables=variables,
        dry_run=True,
    )
