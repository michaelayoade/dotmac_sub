"""CRM API client for DotMac Omni CRM integration.

Provides authenticated HTTP access to tickets, work orders, and subscriber
data in the external CRM system.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, ClassVar, cast

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


# Response cache TTLs (seconds). Portal list/detail pages are read-heavy and
# tolerate brief staleness; caching them stops every page load from fanning
# out live HTTP calls per subscriber account.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


_CACHE_LIST_TTL = _env_int("CRM_CACHE_LIST_SECONDS", 60)
_CACHE_DETAIL_TTL = _env_int("CRM_CACHE_DETAIL_SECONDS", 30)

# Bounded retry for transient rate-limit / unavailable responses. A 429/503
# means the request was rejected (not processed), so retrying — including for
# POSTs — cannot double-apply. Honour the server's Retry-After when present,
# else back off exponentially, each delay capped so total wait stays well
# inside the Celery task time limits.
_RETRY_STATUSES = frozenset({429, 503})
_RETRY_MAX_ATTEMPTS = _env_int("CRM_RETRY_MAX_ATTEMPTS", 2)  # extra tries
_RETRY_MAX_SLEEP = float(_env_int("CRM_RETRY_MAX_SLEEP_SECONDS", 8))


def _resolve_scheduler_int(
    db: Session | None,
    key: str,
    env_name: str,
    default: int,
) -> int:
    value: object | None = None
    if db is not None:
        try:
            value = resolve_value(db, SettingDomain.scheduler, key)
        except Exception:
            value = None
    if value is None:
        return _env_int(env_name, default)
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return _env_int(env_name, default)


def _retry_delay(
    resp: httpx.Response,
    attempt: int,
    *,
    max_sleep: float | None = None,
) -> float:
    """Seconds to wait before the next retry of a 429/503 response.

    Prefers a numeric ``Retry-After`` header; falls back to exponential
    backoff (0.5s, 1s, 2s, …). Always clamped to ``_RETRY_MAX_SLEEP``.
    """
    sleep_cap = _RETRY_MAX_SLEEP if max_sleep is None else max(0.0, max_sleep)
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            return min(sleep_cap, max(0.0, float(retry_after)))
        except ValueError:
            # HTTP-date form is rare here; fall through to backoff.
            pass
    return min(sleep_cap, 0.5 * (2**attempt))


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

    def trip(self, cooldown_seconds: float | None = None) -> None:
        with self._lock:
            cooldown = (
                cooldown_seconds
                if cooldown_seconds is not None
                else float(_env_int("CRM_REACHABILITY_CIRCUIT_SECONDS", 30))
            )
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
        settings_db: Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.settings_db = settings_db
        self._token: str | None = None
        self._token_expires_at: float = 0
        # Cache of minted read-union portal tokens, keyed by (subscriber, actor),
        # so the four mirror reconcilers reuse one token per subscriber per cycle
        # instead of minting four (P4). Value: (token, epoch_expiry).
        self._portal_read_tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._portal_token_lock = threading.RLock()

    @property
    def cache_list_ttl(self) -> int:
        return _resolve_scheduler_int(
            self.settings_db,
            "crm_cache_list_seconds",
            "CRM_CACHE_LIST_SECONDS",
            _CACHE_LIST_TTL,
        )

    @property
    def cache_detail_ttl(self) -> int:
        return _resolve_scheduler_int(
            self.settings_db,
            "crm_cache_detail_seconds",
            "CRM_CACHE_DETAIL_SECONDS",
            _CACHE_DETAIL_TTL,
        )

    @property
    def retry_max_attempts(self) -> int:
        return max(
            0,
            _resolve_scheduler_int(
                self.settings_db,
                "crm_retry_max_attempts",
                "CRM_RETRY_MAX_ATTEMPTS",
                _RETRY_MAX_ATTEMPTS,
            ),
        )

    @property
    def retry_max_sleep(self) -> float:
        return float(
            max(
                0,
                _resolve_scheduler_int(
                    self.settings_db,
                    "crm_retry_max_sleep_seconds",
                    "CRM_RETRY_MAX_SLEEP_SECONDS",
                    int(_RETRY_MAX_SLEEP),
                ),
            )
        )

    @property
    def circuit_seconds(self) -> float:
        return float(
            max(
                1,
                _resolve_scheduler_int(
                    self.settings_db,
                    "crm_reachability_circuit_seconds",
                    "CRM_REACHABILITY_CIRCUIT_SECONDS",
                    30,
                ),
            )
        )

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
            _REACHABILITY_CIRCUIT.trip(self.circuit_seconds)
            logger.error("CRM login error: %s", e)
            raise CRMClientError(f"CRM connection error: {e}") from e

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
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
            attempt = 0
            while True:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(
                        method,
                        url,
                        params=params,
                        json=json_data,
                        headers={
                            "Authorization": f"Bearer {token}",
                            **(headers or {}),
                        },
                    )
                retry_max_attempts = self.retry_max_attempts
                if resp.status_code in _RETRY_STATUSES and attempt < retry_max_attempts:
                    delay = _retry_delay(
                        resp,
                        attempt,
                        max_sleep=self.retry_max_sleep,
                    )
                    logger.warning(
                        "CRM %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        resp.status_code,
                        delay,
                        attempt + 1,
                        retry_max_attempts,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
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
            _REACHABILITY_CIRCUIT.trip(self.circuit_seconds)
            logger.error("CRM request error %s %s: %s", method, path, e)
            raise CRMClientError(f"CRM connection error: {e}") from e

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        content: bytes,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send an unauthenticated raw request with CRM retry/circuit handling."""
        if _REACHABILITY_CIRCUIT.is_open():
            raise CRMClientError("CRM temporarily unavailable (circuit open)")
        if not self.base_url:
            raise CRMClientError("CRM is not configured")

        url = f"{self.base_url}{path}"
        try:
            attempt = 0
            while True:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(
                        method,
                        url,
                        content=content,
                        headers=headers or {},
                    )
                retry_max_attempts = self.retry_max_attempts
                if resp.status_code in _RETRY_STATUSES and attempt < retry_max_attempts:
                    delay = _retry_delay(
                        resp,
                        attempt,
                        max_sleep=self.retry_max_sleep,
                    )
                    logger.warning(
                        "CRM raw %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
                        method,
                        path,
                        resp.status_code,
                        delay,
                        attempt + 1,
                        retry_max_attempts,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                _REACHABILITY_CIRCUIT.reset()
                return resp
        except httpx.RequestError as exc:
            _REACHABILITY_CIRCUIT.trip(self.circuit_seconds)
            logger.error("CRM raw request error %s %s: %s", method, path, exc)
            raise CRMClientError(f"CRM connection error: {exc}") from exc

    def post_signed_webhook(
        self,
        path: str,
        *,
        body: bytes,
        signature: str,
    ) -> httpx.Response:
        return self._raw_request(
            "POST",
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Selfcare-Signature": signature,
            },
        )

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
        """Look up CRM subscriber UUID by legacy external_id.

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
            f"/api/v1/subscribers/{subscriber_id}", None, self.cache_detail_ttl
        )

    def update_subscriber(
        self, subscriber_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Patch fields on a CRM subscriber (e.g. billing snapshot)."""
        return self._request(
            "PATCH", f"/api/v1/subscribers/{subscriber_id}", json_data=payload
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
            self._cached_get("/api/v1/subscribers", params, self.cache_list_ttl)
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
            self._cached_get("/api/v1/tickets", params, self.cache_list_ttl)
            if use_cache
            else self._request("GET", "/api/v1/tickets", params=params)
        )
        return data if isinstance(data, list) else data.get("items", [])

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Get a single ticket by ID."""
        return self._cached_get(
            f"/api/v1/tickets/{ticket_id}", None, self.cache_detail_ttl
        )

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
                self.cache_detail_ttl,
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
        data = self._cached_get("/api/v1/work-orders", params, self.cache_list_ttl)
        return data if isinstance(data, list) else data.get("items", [])

    def get_work_order(self, work_order_id: str) -> dict[str, Any]:
        """Get a single work order by ID."""
        return self._cached_get(
            f"/api/v1/work-orders/{work_order_id}", None, self.cache_detail_ttl
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

    def create_widget_session(
        self,
        *,
        config_id: str,
        email: str,
        name: str | None = None,
        crm_subscriber_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint an already-identified chat_widget visitor session (server-to-server).

        The CRM trusts this caller (authenticated service JWT) to assert the
        visitor's identity, so the client never calls the public, browser-driven
        identify endpoint. Returns {session_id, visitor_token, conversation_id}.
        """
        payload: dict[str, Any] = {"config_id": config_id, "email": email}
        if name:
            payload["name"] = name
        if crm_subscriber_id:
            payload["crm_subscriber_id"] = crm_subscriber_id
        if metadata:
            payload["metadata"] = metadata
        try:
            data = self._request(
                "POST", "/api/v1/widget/internal/session", json_data=payload
            )
            return data if isinstance(data, dict) else {}
        except CRMClientError:
            logger.warning(
                "CRM internal widget session mint failed; falling back to public widget flow"
            )

        origin = os.getenv("APP_URL", "").rstrip("/") or "https://selfcare.dotmac.io"
        session_data = self._request(
            "POST",
            f"/api/v1/widget/{config_id}/session",
            json_data={"page_url": f"{origin}/mobile-chat"},
            headers={"Origin": origin},
        )
        if not isinstance(session_data, dict):
            return {}

        session_id = str(session_data.get("session_id") or "")
        visitor_token = str(session_data.get("visitor_token") or "")
        if not session_id or not visitor_token:
            return session_data

        custom_fields = dict(metadata or {})
        if crm_subscriber_id:
            custom_fields["crm_subscriber_id"] = crm_subscriber_id
        identify_payload: dict[str, Any] = {"email": email}
        if name:
            identify_payload["name"] = name
        if custom_fields:
            identify_payload["custom_fields"] = custom_fields
        identify_data = self._request(
            "POST",
            f"/api/v1/widget/session/{session_id}/identify",
            json_data=identify_payload,
            headers={"Origin": origin, "X-Visitor-Token": visitor_token},
        )
        if isinstance(identify_data, dict) and identify_data.get("conversation_id"):
            session_data["conversation_id"] = identify_data.get("conversation_id")
        return session_data

    def create_portal_session(
        self,
        *,
        crm_subscriber_id: str,
        actor: str = "subscriber",
        scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mint a customer Portal API token (server-to-server, RFC #73).

        The CRM trusts this service JWT to assert the subject, so the client
        never authenticates to the CRM directly — it presents the returned
        short-lived, scoped token. Returns {portal_token, expires_at, api_base}.
        """
        payload: dict[str, Any] = {
            "crm_subscriber_id": crm_subscriber_id,
            "actor": actor,
            "scopes": list(scopes or []),
        }
        data = self._request(
            "POST", "/api/v1/portal/internal/session", json_data=payload
        )
        return data if isinstance(data, dict) else {}

    def _portal_token(
        self, crm_subscriber_id: str, scopes: list[str], actor: str = "subscriber"
    ) -> str:
        minted = self.create_portal_session(
            crm_subscriber_id=crm_subscriber_id, actor=actor, scopes=scopes
        )
        token = str(minted.get("portal_token") or "")
        if not token:
            raise CRMClientError("portal token mint returned an empty token")
        return token

    # The read scopes the four mirror reconcilers collectively need. Minting one
    # token with the union lets a subscriber's referrals/projects/work-orders/
    # quotes reads share it, instead of four separate single-scope mints.
    _PORTAL_READ_SCOPES: ClassVar[list[str]] = [
        "referrals:read",
        "projects:read",
        "work_orders:read",
        "quotes:read",
    ]

    @staticmethod
    def _portal_token_ttl_expiry(minted: dict, now: float) -> float:
        """Epoch time until which a minted portal token may be cached.

        Uses the token's own ``expires_at`` (epoch or ISO-8601) minus a 30s skew
        buffer; falls back to a conservative 60s when it can't be parsed.
        """
        raw = minted.get("expires_at")
        exp: float | None = None
        if isinstance(raw, (int, float)):
            exp = float(raw)
        elif isinstance(raw, str) and raw:
            with contextlib.suppress(ValueError):
                exp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        if exp is None:
            return now + 60.0
        return exp - 30.0

    def _portal_read_token(
        self, crm_subscriber_id: str, actor: str = "subscriber"
    ) -> str:
        """A cached read-union portal token for a subscriber (P4).

        Reused across the four mirror reconcilers so one subscriber costs one
        mint per cycle, not four. Thread-safe; re-mints once the cached token
        nears expiry.
        """
        key = (crm_subscriber_id, actor)
        now = time.time()
        with self._portal_token_lock:
            cached = self._portal_read_tokens.get(key)
            if cached and cached[1] > now:
                return cached[0]

        minted = self.create_portal_session(
            crm_subscriber_id=crm_subscriber_id,
            actor=actor,
            scopes=self._PORTAL_READ_SCOPES,
        )
        token = str(minted.get("portal_token") or "")
        if not token:
            raise CRMClientError("portal token mint returned an empty token")
        with self._portal_token_lock:
            self._portal_read_tokens[key] = (
                token,
                self._portal_token_ttl_expiry(minted, now),
            )
        return token

    def get_portal_referrals(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's referrals from the CRM Portal API (server-side).

        Mints a scoped portal token then calls the portal API with it (the
        per-request ``Authorization`` overrides the service token). Used by the
        local-mirror reconcile, not the customer's own request path.
        """
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/referrals",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def get_portal_projects(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's projects (with derived stages/progress) from the
        CRM Portal API (server-side). Used by the local-mirror reconcile."""
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/projects",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def get_portal_work_orders(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's work orders (technician, schedule, ETA, status)
        from the CRM Portal API (server-side). Used by the local-mirror reconcile."""
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/work-orders",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def get_portal_technician_location(
        self, crm_subscriber_id: str, work_order_id: str, *, actor: str = "subscriber"
    ) -> dict[str, Any]:
        """Live technician position for an in-progress work order (polled). Not
        cached — it's real-time. Returns the CRM's {available, latitude, ...}."""
        token = self._portal_token(crm_subscriber_id, ["work_orders:read"], actor=actor)
        data = self._request(
            "GET",
            f"/api/v1/portal/work-orders/{work_order_id}/technician-location",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def submit_portal_technician_rating(
        self,
        crm_subscriber_id: str,
        work_order_id: str,
        *,
        rating: int,
        comment: str | None = None,
        actor: str = "subscriber",
    ) -> dict[str, Any]:
        """Submit a technician rating for a completed work order (CSAT/Survey)."""
        token = self._portal_token(
            crm_subscriber_id, ["work_orders:write"], actor=actor
        )
        payload: dict[str, Any] = {"rating": rating}
        if comment:
            payload["comment"] = comment
        data = self._request(
            "POST",
            f"/api/v1/portal/work-orders/{work_order_id}/rate-technician",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def create_portal_referral(
        self,
        crm_subscriber_id: str,
        *,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Refer-a-friend write-through to the CRM Portal API (server-side)."""
        token = self._portal_token(crm_subscriber_id, ["referrals:write"])
        payload: dict[str, Any] = {
            k: v
            for k, v in {
                "name": name,
                "email": email,
                "phone": phone,
                "note": note,
            }.items()
            if v
        }
        data = self._request(
            "POST",
            "/api/v1/portal/referrals",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def get_portal_quotes(self, crm_subscriber_id: str) -> dict[str, Any]:
        """Read a subscriber's self-serve quotes (feasibility, estimate, deposit,
        status) from the CRM Portal API (server-side). Used by the mirror reconcile."""
        token = self._portal_read_token(crm_subscriber_id)
        data = self._request(
            "GET",
            "/api/v1/portal/quotes",
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def request_portal_quote(
        self,
        crm_subscriber_id: str,
        *,
        latitude: float,
        longitude: float,
        address: str | None = None,
        region: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Request a map-pinned installation quote (write-through to the CRM
        Portal API). Returns the created quote payload (feasibility + estimate)."""
        token = self._portal_token(crm_subscriber_id, ["quotes:write"])
        payload: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
        if address:
            payload["address"] = address
        if region:
            payload["region"] = region
        if note:
            payload["note"] = note
        data = self._request(
            "POST",
            "/api/v1/portal/quote-request",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def accept_portal_quote(
        self,
        crm_subscriber_id: str,
        quote_id: str,
        *,
        deposit_reference: str,
        deposit_amount: str,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Accept a quote after the deposit is verified (write-through). The CRM
        records the deposit and triggers the sales-order + install-project."""
        token = self._portal_token(crm_subscriber_id, ["quotes:write"])
        payload: dict[str, Any] = {
            "deposit_reference": deposit_reference,
            "deposit_amount": deposit_amount,
        }
        if provider:
            payload["provider"] = provider
        data = self._request(
            "POST",
            f"/api/v1/portal/quotes/{quote_id}/accept",
            json_data=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return data if isinstance(data, dict) else {}

    def list_work_order_notes(self, work_order_id: str) -> list[dict[str, Any]]:
        """List notes for a work order."""
        data = self._cached_get(
            "/api/v1/work-order-notes",
            {"work_order_id": work_order_id, "limit": 500},
            self.cache_detail_ttl,
        )
        return data if isinstance(data, list) else data.get("items", [])


# ── Singleton ────────────────────────────────────────────────────────────

_crm_client: CRMClient | None = None


def get_crm_client(db: Session | None = None) -> CRMClient:
    """Get a CRM client, DB-scoped when runtime settings are available."""
    if db is not None:
        return CRMClient(
            base_url=settings.crm_base_url,
            username=settings.crm_username,
            password=settings.crm_password,
            settings_db=db,
        )
    global _crm_client
    if _crm_client is None:
        _crm_client = CRMClient(
            base_url=settings.crm_base_url,
            username=settings.crm_username,
            password=settings.crm_password,
        )
    return _crm_client
