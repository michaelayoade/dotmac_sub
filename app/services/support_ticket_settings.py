from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service

STATUS_OPTIONS_KEY = "support_ticket_status_options"
PRIORITY_OPTIONS_KEY = "support_ticket_priority_options"
TYPE_OPTIONS_KEY = "support_ticket_type_options"
SETTINGS_DOMAIN = SettingDomain.workflow

DEFAULT_STATUS_OPTIONS = [
    "new",
    "open",
    "pending",
    "waiting_on_customer",
    "lastmile_rerun",
    "site_under_construction",
    "on_hold",
    "resolved",
    "closed",
    "canceled",
    "merged",
]
DEFAULT_PRIORITY_OPTIONS = [
    "lower",
    "low",
    "medium",
    "normal",
    "high",
    "urgent",
]
DEFAULT_TYPE_OPTIONS = [
    "incident",
    "request",
    "change",
    "maintenance",
    "outage",
]
TERMINAL_STATUSES = {"resolved", "closed", "canceled", "merged"}

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _settings_service():
    service = getattr(domain_settings_service, "workflow_settings", None)
    if service is not None:
        return service
    return domain_settings_service.settings


def display_label(value: str) -> str:
    text = str(value or "").strip().replace("_", " ").replace("-", " ")
    return " ".join(part.capitalize() for part in text.split()) or "-"


def normalize_system_value(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _NON_ALNUM_RE.sub("_", text)
    return text.strip("_")


def _normalize_list(
    raw: Any,
    *,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    values = raw if isinstance(raw, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if normalizer is not None:
            text = normalizer(text)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized or list(defaults)


def _read_list(
    db: Session,
    *,
    key: str,
    defaults: list[str],
    normalizer=None,
) -> list[str]:
    service = _settings_service()
    try:
        setting = (
            service.get_by_key(db, key)
            if getattr(service, "domain", None) is not None
            else domain_settings_service.settings.get_by_key(db, key)
        )
    except Exception:
        setting = None
    raw = getattr(setting, "value_json", None)
    return _normalize_list(raw, defaults=defaults, normalizer=normalizer)


def _write_list(
    db: Session,
    *,
    key: str,
    values: list[str],
) -> None:
    payload = DomainSettingUpdate(
        domain=SETTINGS_DOMAIN,
        value_type=SettingValueType.json,
        value_text=None,
        value_json=list(values),
        is_secret=False,
        is_active=True,
    )
    service = _settings_service()
    if getattr(service, "domain", None) is not None:
        service.upsert_by_key(db, key, payload)
        return
    domain_settings_service.settings.upsert_by_key(db, key, payload)


def list_status_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=STATUS_OPTIONS_KEY,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_priority_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=PRIORITY_OPTIONS_KEY,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )


def list_ticket_type_options(db: Session) -> list[str]:
    return _read_list(
        db,
        key=TYPE_OPTIONS_KEY,
        defaults=DEFAULT_TYPE_OPTIONS,
    )


def update_options(
    db: Session,
    *,
    statuses: list[str],
    priorities: list[str],
    ticket_types: list[str],
) -> None:
    normalized_statuses = _normalize_list(
        statuses,
        defaults=DEFAULT_STATUS_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_priorities = _normalize_list(
        priorities,
        defaults=DEFAULT_PRIORITY_OPTIONS,
        normalizer=normalize_system_value,
    )
    normalized_types = _normalize_list(
        ticket_types,
        defaults=DEFAULT_TYPE_OPTIONS,
    )
    _write_list(db, key=STATUS_OPTIONS_KEY, values=normalized_statuses)
    _write_list(db, key=PRIORITY_OPTIONS_KEY, values=normalized_priorities)
    _write_list(db, key=TYPE_OPTIONS_KEY, values=normalized_types)


def default_status(db: Session) -> str:
    options = list_status_options(db)
    return "open" if "open" in options else options[0]


def default_priority(db: Session) -> str:
    options = list_priority_options(db)
    return "normal" if "normal" in options else options[0]


def status_is_terminal(value: str | None) -> bool:
    return str(value or "").strip() in TERMINAL_STATUSES


def status_is_merged(value: str | None) -> bool:
    return str(value or "").strip() == "merged"


def status_color(value: str) -> str:
    variants = {
        "new": "blue",
        "open": "emerald",
        "pending": "amber",
        "waiting_on_customer": "amber",
        "lastmile_rerun": "amber",
        "site_under_construction": "amber",
        "on_hold": "orange",
        "resolved": "teal",
        "closed": "slate",
        "canceled": "red",
        "merged": "violet",
    }
    return variants.get(str(value or "").strip(), "slate")
