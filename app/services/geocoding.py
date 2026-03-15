from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain

logger = logging.getLogger(__name__)

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


def _google_search(db: Session, query: str, limit: int) -> list[dict]:
    api_key = _setting_value(db, "google_api_key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Google geocoding key is not configured")
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    try:
        response = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": api_key},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    body = response.json()
    if body.get("status") not in {"OK", "ZERO_RESULTS"}:
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    results = body.get("results") or []
    if not isinstance(results, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    normalized: list[dict] = []
    for item in results[: max(1, limit)]:
        geometry = item.get("geometry") or {}
        location = geometry.get("location") or {}
        lat = location.get("lat")
        lon = location.get("lng")
        if lat is None or lon is None:
            continue
        normalized.append(
            {
                "display_name": item.get("formatted_address"),
                "lat": lat,
                "lon": lon,
                "class": "google",
                "type": "geocode",
                "importance": 1.0,
            }
        )
    return normalized


def _mapbox_search(db: Session, query: str, limit: int) -> list[dict]:
    token = _setting_value(db, "mapbox_api_key")
    if not token:
        raise HTTPException(status_code=400, detail="Mapbox geocoding token is not configured")
    timeout_sec = _setting_int(db, "timeout_sec", 5)
    try:
        response = httpx.get(
            f"https://api.mapbox.com/geocoding/v5/mapbox.places/{quote(query)}.json",
            params={"access_token": token, "limit": max(limit, 1)},
            timeout=float(timeout_sec),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Geocoding request failed") from exc
    body = response.json()
    features = body.get("features") or []
    if not isinstance(features, list):
        raise HTTPException(status_code=502, detail="Invalid geocoding response")
    normalized: list[dict] = []
    for item in features[: max(1, limit)]:
        center = item.get("center") or []
        if not isinstance(center, list) or len(center) < 2:
            continue
        normalized.append(
            {
                "display_name": item.get("place_name"),
                "lat": center[1],
                "lon": center[0],
                "class": "mapbox",
                "type": item.get("place_type", ["geocode"])[0] if isinstance(item.get("place_type"), list) and item.get("place_type") else "geocode",
                "importance": item.get("relevance"),
            }
        )
    return normalized


def _provider_search(db: Session, query: str, limit: int) -> list[dict]:
    provider = (_setting_value(db, "provider") or "nominatim").strip().lower()
    if provider == "google":
        return _google_search(db, query, limit)
    if provider == "mapbox":
        return _mapbox_search(db, query, limit)
    return _nominatim_search(db, query, limit)


def geocode_address(db: Session, data: dict) -> dict:
    if data.get("latitude") is not None and data.get("longitude") is not None:
        return data
    if not _setting_bool(db, "enabled", True):
        return data
    address = _compose_address(data)
    if not address:
        return data
    results = _provider_search(db, address, 1)
    if not results:
        return data
    first = results[0]
    try:
        data["latitude"] = float(str(first.get("lat") or ""))
        data["longitude"] = float(str(first.get("lon") or ""))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Invalid geocoding response") from exc
    return data


def geocode_preview(db: Session, data: dict, limit: int = 3) -> list[dict]:
    if not _setting_bool(db, "enabled", True):
        return []
    address = _compose_address(data)
    if not address:
        raise HTTPException(status_code=400, detail="Address fields required")
    results = _provider_search(db, address, limit)
    preview: list[dict] = []
    for item in results:
        try:
            preview.append(
                {
                    "display_name": item.get("display_name"),
                    "latitude": float(str(item.get("lat") or "")),
                    "longitude": float(str(item.get("lon") or "")),
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
