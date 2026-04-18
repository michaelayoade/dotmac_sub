from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.services.nin_matching import normalize_nin

MONO_NIN_LOOKUP_PATH = "/v3/lookup/nin"


class MonoNINError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        response_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.response_payload = response_payload


class MonoNINConfigurationError(MonoNINError):
    pass


def _extract_lookup_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _normalized_lookup_response(payload: dict[str, Any]) -> dict[str, Any]:
    data = _extract_lookup_data(payload)
    return {
        "raw": payload,
        "data": {
            "full_name": data.get("full_name") or data.get("name"),
            "date_of_birth": data.get("date_of_birth") or data.get("dob"),
            "phone_number": data.get("phone_number") or data.get("phone"),
        },
    }


def lookup_nin(nin: str) -> dict[str, Any]:
    secret_key = settings.mono_secret_key.strip()
    if not secret_key:
        raise MonoNINConfigurationError(
            "Mono secret key is not configured",
            retryable=False,
        )

    try:
        with httpx.Client(
            base_url=settings.mono_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.mono_timeout_seconds),
        ) as client:
            response = client.post(
                MONO_NIN_LOOKUP_PATH,
                headers={"mono-sec-key": secret_key},
                json={"nin": normalize_nin(nin)},
            )
    except httpx.TimeoutException as exc:
        raise MonoNINError("Mono NIN lookup timed out", retryable=True) from exc
    except httpx.RequestError as exc:
        raise MonoNINError("Mono NIN lookup request failed", retryable=True) from exc

    if response.status_code >= 400:
        retryable = response.status_code == 429 or response.status_code >= 500
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = {"body": response.text[:500]}
        if not isinstance(response_payload, dict):
            response_payload = {"body": str(response_payload)[:500]}
        raise MonoNINError(
            f"Mono NIN lookup failed with HTTP {response.status_code}",
            retryable=retryable,
            response_payload=response_payload,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise MonoNINError("Mono NIN lookup returned invalid JSON", retryable=True) from exc

    if not isinstance(payload, dict):
        raise MonoNINError("Mono NIN lookup returned an unexpected payload", retryable=True)

    return _normalized_lookup_response(payload)
