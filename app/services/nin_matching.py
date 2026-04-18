from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.models.subscriber import Subscriber


def normalize_nin(nin: str) -> str:
    return re.sub(r"\D", "", nin or "")


def validate_nin(nin: str) -> bool:
    return bool(re.fullmatch(r"\d{11}", normalize_nin(nin)))


def mask_nin(nin: str) -> str:
    normalized = normalize_nin(nin)
    if len(normalized) <= 6:
        return "*" * len(normalized)
    return f"{normalized[:6]}{'*' * (len(normalized) - 6)}"


def normalize_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def normalize_phone(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _name_tokens(value: str | None) -> set[str]:
    return {
        normalized
        for part in re.split(r"\s+", (value or "").strip())
        if (normalized := normalize_name(part))
    }


def _name_matches_full_name(expected: str | None, full_name: str | None) -> bool:
    expected_tokens = _name_tokens(expected)
    if not expected_tokens:
        return False
    return expected_tokens.issubset(_name_tokens(full_name))


def _date_string(value: date | str | None) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _normalized_date_string(value: date | str | None) -> str:
    raw = _date_string(value)
    if not raw:
        return ""
    for candidate in (raw, raw[:10]):
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return raw


def match_subscriber_nin_response(
    subscriber: Subscriber,
    mono_data: dict[str, Any],
) -> dict[str, int | bool]:
    mono_full_name = str(mono_data.get("full_name") or "")
    first_match = _name_matches_full_name(subscriber.first_name, mono_full_name)
    last_match = _name_matches_full_name(subscriber.last_name, mono_full_name)
    name_match = first_match and last_match
    dob_match = _normalized_date_string(
        subscriber.date_of_birth
    ) == _normalized_date_string(mono_data.get("date_of_birth"))

    subscriber_phone = normalize_phone(subscriber.phone)
    mono_phone = normalize_phone(str(mono_data.get("phone_number") or ""))
    phone_match = bool(
        subscriber_phone and mono_phone and subscriber_phone == mono_phone
    )

    score = 0
    if name_match:
        score += 50
    if dob_match:
        score += 40
    if phone_match:
        score += 10

    return {
        "is_match": bool(first_match and last_match and dob_match),
        "match_score": score,
    }
