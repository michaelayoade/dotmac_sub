from __future__ import annotations

import re
from collections.abc import Mapping

_MEDIA_PLACEHOLDER_RE = re.compile(
    r"^\[?(?:image|photo|picture|screenshot|document|file|attachment|media|"
    r"audio|voice(?: note)?|video|sticker|location)\]?"
    r"(?:[ .:_-]*(?:sent|attached|message|file|attachment))?$",
    re.IGNORECASE,
)
_MEDIA_FILENAME_RE = re.compile(
    r"^[\w .()_-]{1,160}\."
    r"(?:jpe?g|png|gif|webp|heic|pdf|docx?|xlsx?|mp3|m4a|ogg|wav|mp4|mov)$",
    re.IGNORECASE,
)
_MEDIA_PLACEHOLDER_TOKENS = frozenset(
    {
        "attachment",
        "audio",
        "document",
        "file",
        "image",
        "location",
        "media",
        "message",
        "photo",
        "picture",
        "screenshot",
        "sent",
        "sticker",
        "video",
        "voice",
        "note",
    }
)
_HUMAN_IMPERSONATION_RE = re.compile(
    r"\b(?:tell|say|claim|pretend|act as|identify as|introduce yourself as)\b"
    r".{0,160}\b(?:human|human agent|staff member|employee|person|real agent|"
    r"support agent|john from support)\b|"
    r"\b(?:pretend|claim|say|tell)\b.{0,120}\b(?:not automated|not a bot|not ai)\b|"
    r"\b(?:i am|i'm|you are|you're)\s+[a-z]{2,40}\s+from\s+support\b",
    re.IGNORECASE,
)


def usable_customer_text(value: str | None) -> str | None:
    """Return meaningful customer text, excluding provider media placeholders."""

    text = " ".join(str(value or "").split())
    if not text:
        return None
    simplified = text.strip().strip("[](){}<>").strip(" .:_-").casefold()
    if not simplified or not any(character.isalnum() for character in simplified):
        return None
    if _MEDIA_FILENAME_RE.fullmatch(simplified):
        return None
    if _MEDIA_PLACEHOLDER_RE.fullmatch(simplified):
        return None
    tokens = re.findall(r"[a-z0-9]+", simplified)
    if tokens and all(token in _MEDIA_PLACEHOLDER_TOKENS for token in tokens):
        return None
    return text


def human_impersonation_violations(fields: Mapping[str, object]) -> tuple[str, ...]:
    """Return customer-facing policy fields that attempt human impersonation."""

    violations: list[str] = []
    for field, value in fields.items():
        if value is None:
            continue
        text = " ".join(str(value).split())
        if not text:
            continue
        if _HUMAN_IMPERSONATION_RE.search(text):
            violations.append(field)
    return tuple(violations)
