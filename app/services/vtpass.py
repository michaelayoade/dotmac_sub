"""VTPass API adapter (sandbox-ready).

Thin HTTP wrapper — all business logic (state machine, wallet, refunds)
lives in vas_purchases. Auth per VTPass docs: api-key + secret-key headers
for POST, api-key + public-key for GET.

request_id quirk: VTPass requires the id to START with the current
date/time in Africa/Lagos as YYYYMMDDHHMM, then any unique suffix.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

logger = logging.getLogger(__name__)

SANDBOX_BASE_URL = "https://sandbox.vtpass.com/api"
# Lagos is UTC+1, no DST.
_LAGOS_OFFSET = timedelta(hours=1)
DEFAULT_GET_TIMEOUT_SECONDS = 20.0
DEFAULT_POST_TIMEOUT_SECONDS = 45.0
MIN_TIMEOUT_SECONDS = 1.0
MAX_TIMEOUT_SECONDS = 120.0


def _setting(db: Session, key: str) -> str | None:
    value = settings_spec.resolve_value(db, SettingDomain.vas, key)
    return str(value) if value not in (None, "") else None


def _timeout(db: Session, key: str, default: float) -> float:
    raw = _setting(db, key)
    try:
        parsed = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, parsed))


def _base_url(db: Session) -> str:
    return (_setting(db, "vtpass_base_url") or SANDBOX_BASE_URL).rstrip("/")


def _get_headers(db: Session) -> dict[str, str]:
    api_key = _setting(db, "vtpass_api_key")
    public_key = _setting(db, "vtpass_public_key")
    if not api_key or not public_key:
        raise HTTPException(status_code=503, detail="Bill payments are not configured")
    return {"api-key": api_key, "public-key": public_key}


def _post_headers(db: Session) -> dict[str, str]:
    api_key = _setting(db, "vtpass_api_key")
    secret_key = _setting(db, "vtpass_secret_key")
    if not api_key or not secret_key:
        raise HTTPException(status_code=503, detail="Bill payments are not configured")
    return {"api-key": api_key, "secret-key": secret_key}


def generate_request_id() -> str:
    lagos_now = datetime.now(UTC) + _LAGOS_OFFSET
    return f"{lagos_now:%Y%m%d%H%M}{uuid.uuid4().hex[:12]}"


def _get(db: Session, path: str, params: dict | None = None) -> dict:
    try:
        response = httpx.get(
            f"{_base_url(db)}/{path}",
            params=params,
            headers=_get_headers(db),
            timeout=_timeout(
                db, "vtpass_get_timeout_seconds", DEFAULT_GET_TIMEOUT_SECONDS
            ),
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("vtpass GET %s failed: %s", path, exc)
        raise HTTPException(
            status_code=502, detail="Bill payment provider unavailable"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid provider response")
    return body


def _post(
    db: Session,
    path: str,
    payload: dict,
    *,
    timeout: float | None = None,
    timeout_key: str = "vtpass_post_timeout_seconds",
) -> dict:
    try:
        response = httpx.post(
            f"{_base_url(db)}/{path}",
            json=payload,
            headers=_post_headers(db),
            timeout=timeout
            if timeout is not None
            else _timeout(db, timeout_key, DEFAULT_POST_TIMEOUT_SECONDS),
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPError as exc:
        logger.warning("vtpass POST %s failed: %s", path, exc)
        raise HTTPException(
            status_code=502, detail="Bill payment provider unavailable"
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid provider response")
    return body


# --- Catalog ------------------------------------------------------------------


def get_service_categories(db: Session) -> list[dict]:
    body = _get(db, "service-categories")
    return list(body.get("content") or [])


def get_services(db: Session, identifier: str) -> list[dict]:
    body = _get(db, "services", {"identifier": identifier})
    return list(body.get("content") or [])


def get_variations(db: Session, service_id: str) -> dict:
    body = _get(db, "service-variations", {"serviceID": service_id})
    content = body.get("content") or {}
    return content if isinstance(content, dict) else {}


# --- Verify / balance -----------------------------------------------------------


def verify_merchant(
    db: Session, *, service_id: str, billers_code: str, variation_type: str | None
) -> dict:
    payload: dict[str, Any] = {"serviceID": service_id, "billersCode": billers_code}
    if variation_type:
        payload["type"] = variation_type
    body = _post(
        db,
        "merchant-verify",
        payload,
        timeout_key="vtpass_verify_timeout_seconds",
    )
    content = body.get("content")
    if not isinstance(content, dict) or content.get("error"):
        detail = (
            content.get("error")
            if isinstance(content, dict)
            else "Could not verify the customer number"
        )
        raise HTTPException(status_code=400, detail=str(detail))
    return content


def get_balance(db: Session) -> Decimal:
    body = _get(db, "balance")
    contents = body.get("contents") or {}
    try:
        return Decimal(str(contents.get("balance") or "0"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail="Invalid provider response"
        ) from exc


# --- Pay / requery ---------------------------------------------------------------

# VTPass response codes (the canonical ones we act on)
CODE_DELIVERED = "000"
CODE_PROCESSING = "099"


def pay(
    db: Session,
    *,
    request_id: str,
    service_id: str,
    billers_code: str | None,
    variation_code: str | None,
    amount: Decimal | None,
    phone: str,
) -> dict:
    payload: dict[str, Any] = {
        "request_id": request_id,
        "serviceID": service_id,
        "phone": phone,
    }
    if billers_code:
        payload["billersCode"] = billers_code
    if variation_code:
        payload["variation_code"] = variation_code
    if amount is not None:
        payload["amount"] = float(amount)
    return _post(db, "pay", payload)


def requery(db: Session, request_id: str) -> dict:
    return _post(
        db,
        "requery",
        {"request_id": request_id},
        timeout_key="vtpass_requery_timeout_seconds",
    )
