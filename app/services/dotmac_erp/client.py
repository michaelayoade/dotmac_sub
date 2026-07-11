from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec


class DotMacERPError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class DotMacERPAuthError(DotMacERPError):
    pass


class DotMacERPNotFoundError(DotMacERPError):
    pass


class DotMacERPTransientError(DotMacERPError):
    pass


class DotMacERPClient:
    """Small DotMac ERP client for field-service sync endpoints."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> DotMacERPClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def push_material_request(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/sync/crm/material-requests",
            json_data=payload,
            idempotency_key=idempotency_key or f"field-mr-{uuid4()}",
            expected_status_codes={200, 201},
        )
        return response if isinstance(response, dict) else {}

    def push_expense_claim(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/sync/crm/expense-claims",
            json_data=payload,
            idempotency_key=idempotency_key or f"field-exp-{uuid4()}",
            expected_status_codes={200, 201},
        )
        return response if isinstance(response, dict) else {}

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "X-API-Key": self.token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "DotMac-Sub/1.0",
                },
            )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | list[Any] | None = None,
        idempotency_key: str | None = None,
        expected_status_codes: Collection[int] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = self._http().request(
                method,
                path,
                json=json_data,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise DotMacERPTransientError(f"ERP transport error: {exc}") from exc
        return _handle_response(response, expected_status_codes=expected_status_codes)


def _handle_response(
    response: httpx.Response,
    *,
    expected_status_codes: Collection[int] | None = None,
) -> dict[str, Any] | list[Any] | None:
    try:
        data = response.json() if response.content else None
    except ValueError:
        data = None

    body = data if isinstance(data, dict) else None
    if response.status_code in {401, 403}:
        raise DotMacERPAuthError(
            f"ERP authentication failed: {response.status_code}",
            status_code=response.status_code,
            response=body,
        )
    if response.status_code == 404:
        raise DotMacERPNotFoundError(
            "ERP resource not found",
            status_code=404,
            response=body,
        )
    if response.status_code == 429 or response.status_code >= 500:
        raise DotMacERPTransientError(
            f"ERP transient error: {response.status_code}",
            status_code=response.status_code,
            response=body,
        )
    if expected_status_codes and response.status_code not in expected_status_codes:
        raise DotMacERPError(
            f"ERP unexpected status: {response.status_code}",
            status_code=response.status_code,
            response=body,
        )
    if response.status_code >= 400:
        message = (
            body.get("detail") or body.get("message") or body.get("error")
            if body
            else response.text
        )
        raise DotMacERPError(
            f"ERP error {response.status_code}: {message}",
            status_code=response.status_code,
            response=body,
        )
    return data


def dotmac_erp_client_from_settings(db: Session) -> DotMacERPClient:
    base_url = str(
        settings_spec.resolve_value(
            db, SettingDomain.integration, "dotmac_erp_base_url"
        )
        or ""
    ).strip()
    token = str(
        settings_spec.resolve_value(db, SettingDomain.integration, "dotmac_erp_token")
        or ""
    ).strip()
    timeout = int(
        settings_spec.resolve_value(
            db, SettingDomain.integration, "dotmac_erp_timeout_seconds"
        )
        or 30
    )
    if not base_url or not token:
        raise ValueError("DotMac ERP is not configured")
    return DotMacERPClient(base_url=base_url, token=token, timeout=timeout)
