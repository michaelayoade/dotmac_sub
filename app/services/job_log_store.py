"""Shared helpers for persisted background job logs."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate


def read_json_list(
    db: Session, settings_service: Any, key: str
) -> list[dict[str, Any]]:
    """Read a JSON list setting and return only dict rows.

    Returns empty list if setting doesn't exist (common before first job runs).
    """
    try:
        setting = settings_service.get_by_key(db, key)
    except Exception:
        # Setting may not exist before first job runs - this is normal
        return []
    if isinstance(setting.value_json, list):
        return [item for item in setting.value_json if isinstance(item, dict)]
    if isinstance(setting.value_text, str) and setting.value_text.strip():
        try:
            parsed = json.loads(setting.value_text)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    return []


def save_json_list(
    db: Session,
    settings_service: Any,
    key: str,
    rows: list[dict[str, Any]],
    *,
    limit: int,
    is_secret: bool = False,
    is_active: bool | None = None,
) -> None:
    """Persist trimmed JSON rows into a domain setting."""
    payload: dict[str, Any] = {
        "value_type": SettingValueType.json,
        "value_json": rows[: max(1, limit)],
        "value_text": None,
        "is_secret": is_secret,
    }
    if is_active is not None:
        payload["is_active"] = is_active
    settings_service.upsert_by_key(db, key, DomainSettingUpdate(**payload))


def get_job(rows: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("job_id") or "") == job_id:
            return row
    return None


def upsert_job(
    rows: list[dict[str, Any]], payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    normalized_payload = {**payload, "job_id": job_id}
    for idx, row in enumerate(rows):
        if str(row.get("job_id") or "") == job_id:
            merged = {**row, **normalized_payload}
            rows.pop(idx)
            rows.insert(0, merged)
            return rows, merged
    rows.insert(0, normalized_payload)
    return rows, normalized_payload
