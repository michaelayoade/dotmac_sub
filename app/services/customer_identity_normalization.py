"""Authoritative identity normalization helpers."""

from __future__ import annotations

import re

IDENTITY_TYPE_EMAIL = "email"
IDENTITY_TYPE_PHONE = "phone"
IDENTITY_TYPE_WHATSAPP = "whatsapp"
IDENTITY_TYPE_SMS = "sms"

PHONE_LIKE_HINTS = {
    IDENTITY_TYPE_PHONE,
    IDENTITY_TYPE_WHATSAPP,
    IDENTITY_TYPE_SMS,
    "chat",
    "tel",
}
DEFAULT_COUNTRY_CODE = "234"


def default_country_code(db=None) -> str:
    if db is None:
        return DEFAULT_COUNTRY_CODE
    try:
        from app.models.domain_settings import SettingDomain
        from app.services.settings_spec import resolve_value

        value = resolve_value(db, SettingDomain.subscriber, "default_country_code")
    except Exception:
        value = None
    normalized = re.sub(r"\D", "", str(value or "").strip())
    return normalized or DEFAULT_COUNTRY_CODE


def normalize_email_identifier(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def normalize_phone_identifier(
    value: str | None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    raw = raw.replace("\u00a0", " ")
    lowered = raw.lower()
    for prefix in ("whatsapp:", "sms:", "tel:"):
        if lowered.startswith(prefix):
            raw = raw.split(":", 1)[1].strip()
            lowered = raw.lower()
            break

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return f"+{digits}"
    if digits.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith(default_country_code):
        return f"+{digits}"
    if digits.startswith("0") and len(digits) >= 10:
        return f"+{default_country_code}{digits[1:]}"
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    return f"+{digits}"


def normalize_identifier(
    value: str | None,
    hint: str | None = None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    text = str(value or "")
    normalized_hint = str(hint or "").strip().lower()
    if normalized_hint == IDENTITY_TYPE_EMAIL or "@" in text:
        return normalize_email_identifier(text)
    return normalize_phone_identifier(text, default_country_code=default_country_code)


def normalize_channel_address(
    channel_type: str | None,
    value: str | None,
    *,
    default_country_code: str = DEFAULT_COUNTRY_CODE,
) -> str | None:
    normalized_channel = str(channel_type or "").strip().lower()
    if normalized_channel == IDENTITY_TYPE_EMAIL:
        return normalize_email_identifier(value)
    if normalized_channel in PHONE_LIKE_HINTS:
        return normalize_phone_identifier(value, default_country_code=default_country_code)
    return normalize_identifier(
        value, normalized_channel, default_country_code=default_country_code
    )
