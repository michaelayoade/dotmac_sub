"""CRM API client for DotMac Omni CRM integration.

Provides authenticated HTTP access to tickets, work orders, and subscriber
data in the external CRM system.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class CRMClientError(Exception):
    """Base exception for CRM client errors."""


class CRMClient:
    """HTTP client for the DotMac Omni CRM API.

    Handles JWT authentication with automatic token refresh,
    and provides typed methods for each CRM resource.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at: float = 0

    def _ensure_token(self) -> str:
        """Get a valid JWT token, refreshing if within 60s of expiry."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        if not self.base_url or not self.username or not self.password:
            raise CRMClientError("CRM is not configured")

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/api/v1/auth/login",
                    json={"username": self.username, "password": self.password},
                )
                resp.raise_for_status()
                data = resp.json()
                self._token = data["access_token"]
                # Token lasts 15min; cache for 14min
                self._token_expires_at = time.time() + 840
                return self._token
        except httpx.HTTPStatusError as e:
            logger.error(
                "CRM login failed: %d %s", e.response.status_code, e.response.text[:200]
            )
            raise CRMClientError(f"CRM login failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error("CRM login error: %s", e)
            raise CRMClientError(f"CRM connection error: {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        """Make an authenticated request to the CRM API.

        Returns:
            Parsed JSON response.

        Raises:
            CRMClientError: On any request failure.
        """
        token = self._ensure_token()
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                if not resp.text:
                    return {}
                return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(
                "CRM API error %s %s: %d %s",
                method,
                path,
                e.response.status_code,
                e.response.text[:200],
            )
            raise CRMClientError(f"CRM API error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error("CRM request error %s %s: %s", method, path, e)
            raise CRMClientError(f"CRM connection error: {e}") from e

    # ── Subscriber resolution ────────────────────────────────────────────

    def resolve_subscriber_id(self, splynx_customer_id: int) -> str | None:
        """Look up CRM subscriber UUID by Splynx external_id.

        Returns:
            CRM subscriber UUID string, or None if not found.
        """
        try:
            data = self._request(
                "GET",
                "/api/v1/subscribers",
                params={
                    "external_id": str(splynx_customer_id),
                    "external_system": "splynx",
                    "limit": 1,
                },
            )
            items = data if isinstance(data, list) else data.get("items", [])
            if items:
                return str(items[0]["id"])
        except CRMClientError:
            logger.warning(
                "CRM subscriber lookup failed for splynx_id=%s", splynx_customer_id
            )
        return None

    # ── Tickets ──────────────────────────────────────────────────────────

    def list_tickets(self, subscriber_id: str | None = None) -> list[dict[str, Any]]:
        """List tickets, optionally filtered by CRM subscriber."""
        params: dict[str, Any] = {"limit": 100}
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        data = self._request("GET", "/api/v1/tickets", params=params)
        return data if isinstance(data, list) else data.get("items", [])

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get a single ticket by ID."""
        return self._request("GET", f"/api/v1/tickets/{ticket_id}")

    def create_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new ticket in the CRM."""
        return self._request("POST", "/api/v1/tickets", json_data=payload)

    def list_ticket_comments(self, ticket_id: str) -> list[dict[str, Any]]:
        """List comments for a ticket."""
        data = self._request(
            "GET",
            "/api/v1/ticket-comments",
            params={"ticket_id": ticket_id, "limit": 500},
        )
        return data if isinstance(data, list) else data.get("items", [])

    def create_ticket_comment(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a comment to a ticket."""
        return self._request("POST", "/api/v1/ticket-comments", json_data=payload)

    # ── Work Orders ──────────────────────────────────────────────────────

    def list_work_orders(
        self, subscriber_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List work orders, optionally filtered by CRM subscriber."""
        params: dict[str, Any] = {"limit": 100}
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        data = self._request("GET", "/api/v1/work-orders", params=params)
        return data if isinstance(data, list) else data.get("items", [])

    def get_work_order(self, work_order_id: str) -> dict[str, Any]:
        """Get a single work order by ID."""
        return self._request("GET", f"/api/v1/work-orders/{work_order_id}")

    def list_work_order_notes(self, work_order_id: str) -> list[dict[str, Any]]:
        """List notes for a work order."""
        data = self._request(
            "GET",
            "/api/v1/work-order-notes",
            params={"work_order_id": work_order_id, "limit": 500},
        )
        return data if isinstance(data, list) else data.get("items", [])


# ── Singleton ────────────────────────────────────────────────────────────

_crm_client: CRMClient | None = None


def get_crm_client() -> CRMClient:
    """Get or create the singleton CRM client instance."""
    global _crm_client
    if _crm_client is None:
        _crm_client = CRMClient(
            base_url=settings.crm_base_url,
            username=settings.crm_username,
            password=settings.crm_password,
        )
    return _crm_client
