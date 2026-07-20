"""Transport-neutral semantic presentation for domain status values."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class StatusTone(StrEnum):
    """Meaning carried across clients; each client owns its visual tokens."""

    positive = "positive"
    info = "info"
    warning = "warning"
    negative = "negative"
    neutral = "neutral"


class StatusIcon(StrEnum):
    """Small, code-native icon vocabulary for non-color status distinction."""

    check = "check"
    info = "info"
    clock = "clock"
    alert = "alert"
    x = "x"
    minus = "minus"
    archive = "archive"


class StatusPresentation(BaseModel):
    """Server-owned label and semantics for one authoritative status value."""

    value: str
    label: str
    tone: StatusTone
    icon: StatusIcon
