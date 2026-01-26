from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain


def _setting_value(db: Session, key: str) -> str | None:
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.geocoding)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text is not None:
        return setting.value_text
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _setting_bool(db: Session, key: str, default: bool) -> bool:
    raw = _setting_value(db, key)
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _setting_int(db: Session, key: str, default: int) -> int:
    raw = _setting_value(db, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _compose_address(data: dict) -> str | None:
    parts = [
        data.get("address_line1"),
        data.get("address_line2"),
        data.get("city"),
        data.get("region"),
        data.get("postal_code"),
        data.get("country_code"),
    ]
    value = ", ".join([part for part in parts if part])
    return value.strip() if value else None


def _nominatim_search(db: Session, query: str, limit: int) -> list[dict]:
    base_url = _setting_value(db, "base_url") or "https://nominatim.openstreetmap.org"
    user_agent = _setting_value(db, "user_agent") or "dotmac_sm"
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    email = _setting_value(db, "email")
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "limit": max(limit, 1),
    }
    if email:
        params["email"] = email
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/search",
            params=params,
            headers={"User-Agent": user_agent},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    results = response.json()
    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    return results


def geocode_address(db: Session, data: dict) -> dict:
    if data.get("latitude") is not None and data.get("longitude") is not None:
        return data
    if not _setting_bool(db, "enabled", True):
        return data
    provider = _setting_value(db, "provider") or "nominatim"
    if provider != "nominatim":
        return data
    address = _compose_address(data)
    if not address:
        return data
    results = _nominatim_search(db, address, 1)
    if not results:
        return data
    first = results[0]
    try:
        data["latitude"] = float(first.get("lat"))
        data["longitude"] = float(first.get("lon"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid geocoding response") from exc
    return data


def geocode_preview(db: Session, data: dict, limit: int = 3) -> list[dict]:
    if not _setting_bool(db, "enabled", True):
        return []
    provider = _setting_value(db, "provider") or "nominatim"
    if provider != "nominatim":
        return []
    address = _compose_address(data)
    if not address:
        raise HTTPException(status_code=400, detail="Address fields required")
    results = _nominatim_search(db, address, limit)
    preview: list[dict] = []
    for item in results:
        try:
            preview.append(
                {
                    "display_name": item.get("display_name"),
                    "latitude": float(item.get("lat")),
                    "longitude": float(item.get("lon")),
                    "class": item.get("class"),
                    "type": item.get("type"),
                    "importance": item.get("importance"),
                }
            )
        except (TypeError, ValueError):
            continue
    return preview


def geocode_preview_from_request(db: Session, payload) -> list[dict]:
    data = payload.model_dump(exclude={"limit"})
    return geocode_preview(db, data, limit=payload.limit or 3)
