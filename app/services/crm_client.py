"""CRM API client for DotMac Omni CRM integration.

Provides authenticated HTTP access to tickets, work orders, and subscriber
data in the external CRM system.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Any, cast

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Response cache TTLs (seconds). Portal list/detail pages are read-heavy and
# tolerate brief staleness; caching them stops every page load from fanning
# out live HTTP calls per subscriber account.
_CACHE_LIST_TTL = int(os.getenv("CRM_CACHE_LIST_SECONDS", "60"))
_CACHE_DETAIL_TTL = int(os.getenv("CRM_CACHE_DETAIL_SECONDS", "30"))


class CRMClientError(Exception):
    """Base exception for CRM client errors."""


class _CRMReachabilityCircuit:
    """Short-cooldown breaker tripped by connection/timeout failures.

    A slow or unreachable CRM otherwise makes every per-subscriber request wait
    out the full HTTP timeout; the reseller dashboard and work-order pages fan
    out across N accounts, turning one outage into N × timeout. The first
    connection failure trips this breaker so the remaining requests in the same
    window fast-fail instead of each blocking for the full timeout. A single
    successful call closes it again.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def trip(self) -> None:
        with self._lock:
            cooldown = float(os.getenv("CRM_REACHABILITY_CIRCUIT_SECONDS", "30"))
            self._open_until = time.monotonic() + max(cooldown, 1.0)

    def reset(self) -> None:
        with self._lock:
            self._open_until = 0.0


_REACHABILITY_CIRCUIT = _CRMReachabilityCircuit()


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

        if _REACHABILITY_CIRCUIT.is_open():
            raise CRMClientError("CRM temporarily unavailable (circuit open)")

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
                _REACHABILITY_CIRCUIT.reset()
                return self._token
        except httpx.HTTPStatusError as e:
            logger.error(
                "CRM login failed: %d %s", e.response.status_code, e.response.text[:200]
            )
            raise CRMClientError(f"CRM login failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            # Connection/timeout — CRM is unreachable, trip the breaker so the
            # rest of this request's fan-out fast-fails.
            _REACHABILITY_CIRCUIT.trip()
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
        if _REACHABILITY_CIRCUIT.is_open():
            raise CRMClientError("CRM temporarily unavailable (circuit open)")

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
                _REACHABILITY_CIRCUIT.reset()
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
            # Connection/timeout — CRM is unreachable, trip the breaker so the
            # rest of this request's fan-out fast-fails.
            _REACHABILITY_CIRCUIT.trip()
            logger.error("CRM request error %s %s: %s", method, path, e)
            raise CRMClientError(f"CRM connection error: {e}") from e

    def _cached_get(
        self,
        path: str,
        params: dict[str, Any] | None,
        ttl: int,
    ) -> Any:
        """GET with a short-lived Redis response cache.

        On cache hit, returns the stored JSON without touching the CRM. On miss,
        performs the request and caches a successful result for ``ttl`` seconds.
        Redis being unavailable degrades transparently to a live request.
        """
        from app.services.session_store import get_session_redis

        redis = get_session_redis()
        cache_key: str | None = None
        if redis is not None and ttl > 0:
            raw_key = json.dumps([path, params or {}], sort_keys=True)
            digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            cache_key = f"crm:resp:{digest}"
            try:
                cached = redis.get(cache_key)
                if cached is not None:
                    return json.loads(cast("str | bytes | bytearray", cached))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CRM cache get failed for %s: %s", path, exc)

        data = self._request("GET", path, params=params)

        if redis is not None and cache_key is not None:
            try:
                redis.setex(cache_key, ttl, json.dumps(data))
            except Exception as exc:  # noqa: BLE001
                logger.warning("CRM cache set failed for %s: %s", path, exc)
        return data

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
                    "search": str(splynx_customer_id),
                    "external_system": "splynx",
                    "per_page": 10,
                },
            )
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                if str(item.get("external_id") or "") == str(splynx_customer_id):
                    return str(item["id"])
        except CRMClientError:
            logger.warning(
                "CRM subscriber lookup failed for splynx_id=%s", splynx_customer_id
            )
        return None

    def get_subscriber(self, subscriber_id: str) -> dict[str, Any]:
        """Get a CRM subscriber by UUID."""
        return self._cached_get(
            f"/api/v1/subscribers/{subscriber_id}", None, _CACHE_DETAIL_TTL
        )

    def list_subscribers(
        self,
        *,
        external_system: str | None = None,
        page: int = 1,
        per_page: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List CRM subscribers with CRM's page/per_page pagination."""
        params: dict[str, Any] = {
            "page": max(page, 1),
            "per_page": min(max(per_page, 1), 100),
        }
        if external_system:
            params["external_system"] = external_system
        data = (
            self._cached_get("/api/v1/subscribers", params, _CACHE_LIST_TTL)
            if use_cache
            else self._request("GET", "/api/v1/subscribers", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    # ── Tickets ──────────────────────────────────────────────────────────

    def list_tickets(
        self,
        subscriber_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created_at",
        order_dir: str = "desc",
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List tickets, optionally filtered by CRM subscriber."""
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 200),
            "offset": max(offset, 0),
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        data = (
            self._cached_get("/api/v1/tickets", params, _CACHE_LIST_TTL)
            if use_cache
            else self._request("GET", "/api/v1/tickets", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get a single ticket by ID."""
        return self._cached_get(f"/api/v1/tickets/{ticket_id}", None, _CACHE_DETAIL_TTL)

    def create_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new ticket in the CRM."""
        return self._request("POST", "/api/v1/tickets", json_data=payload)

    def update_ticket(self, ticket_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Patch fields on a CRM ticket (e.g. re-point its subscriber)."""
        return self._request("PATCH", f"/api/v1/tickets/{ticket_id}", json_data=payload)

    def list_ticket_comments(
        self, ticket_id: str, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        """List comments for a ticket."""
        params = {"ticket_id": ticket_id, "limit": 200}
        data = (
            self._cached_get(
                "/api/v1/ticket-comments",
                params,
                _CACHE_DETAIL_TTL,
            )
            if use_cache
            else self._request("GET", "/api/v1/ticket-comments", params=params)
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
        data = self._cached_get("/api/v1/work-orders", params, _CACHE_LIST_TTL)
        return data if isinstance(data, list) else data.get("items", [])

    def get_work_order(self, work_order_id: str) -> dict[str, Any]:
        """Get a single work order by ID."""
        return self._cached_get(
            f"/api/v1/work-orders/{work_order_id}", None, _CACHE_DETAIL_TTL
        )

    def update_work_order(
        self, work_order_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Patch fields on a CRM work order."""
        return self._request(
            "PATCH", f"/api/v1/work-orders/{work_order_id}", json_data=payload
        )

    def delete_subscriber(self, subscriber_id: str) -> None:
        """Soft-delete a CRM subscriber (sets is_active=False CRM-side)."""
        self._request("DELETE", f"/api/v1/subscribers/{subscriber_id}")

    def list_work_order_notes(self, work_order_id: str) -> list[dict[str, Any]]:
        """List notes for a work order."""
        data = self._cached_get(
            "/api/v1/work-order-notes",
            {"work_order_id": work_order_id, "limit": 500},
            _CACHE_DETAIL_TTL,
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
