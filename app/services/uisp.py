"""Read-only UISP (Ubiquiti network controller) NMS API client.

Follows the shared external-API client conventions: base-URL/token
resolution (file -> env -> OpenBao), request timeout from env, and a
short-cooldown reachability circuit breaker so an unreachable UISP fast-fails
instead of stacking full HTTP timeouts.

This client is strictly READ-ONLY: it only issues GET requests against the
NMS v2.1 API and exposes no write helpers. Topology imports flow one way,
UISP -> sub's own tables (see ``app/services/topology/uisp_sync.py``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UispClientError(Exception):
    pass


class UispConfigurationError(UispClientError):
    pass


class UispAuthError(UispClientError):
    pass


DEFAULT_UISP_API_URL = "https://uisp.dotmac.ng"
# NMS API v2.1 path prefix, appended to the resolved base URL.
NMS_API_PREFIX = "/nms/api/v2.1"


class _UispReachabilityCircuit:
    """Short-cooldown breaker tripped by connection/timeout failures.

    One transport
    failure trips the breaker so the remaining requests in the same window
    fast-fail instead of each waiting out the full HTTP timeout.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open_until = 0.0

    def is_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def trip(self) -> None:
        with self._lock:
            cooldown = float(os.getenv("UISP_REACHABILITY_CIRCUIT_SECONDS", "30"))
            self._open_until = time.monotonic() + max(cooldown, 1.0)


_REACHABILITY_CIRCUIT = _UispReachabilityCircuit()


def _read_secret_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning(
            "uisp_secret_file_unreadable",
            extra={"event": "uisp_secret_file_unreadable", "path": path},
        )
        return ""


def get_uisp_api_url() -> str:
    """Resolve the configured UISP base URL (no API path suffix)."""
    from app.services.secrets import resolve_secret

    configured = resolve_secret(os.getenv("UISP_API_URL")) or DEFAULT_UISP_API_URL
    return str(configured).rstrip("/")


def get_uisp_api_token() -> str:
    """Resolve the UISP API token: file -> env -> OpenBao fallback.

    ``UISP_API_TOKEN_FILE`` wins,
    then ``UISP_API_TOKEN`` (with ``bao://`` reference resolution), then the
    OpenBao ``uisp/api_token`` secret. Returns "" when unconfigured.
    """
    from app.services.secrets import get_secret, resolve_secret

    file_value = _read_secret_file(os.getenv("UISP_API_TOKEN_FILE"))
    if file_value:
        return file_value

    env_value = os.getenv("UISP_API_TOKEN")
    if env_value:
        return str(resolve_secret(env_value) or "")

    bao_value = get_secret("uisp", "api_token", default="")
    if bao_value:
        return bao_value

    return ""


def uisp_configured() -> bool:
    """Return true when the UISP API has enough config to be used."""
    try:
        return bool(get_uisp_api_url() and get_uisp_api_token())
    except Exception:
        logger.debug("uisp_config_resolution_failed", exc_info=True)
        return False


class UispClient:
    """Typed, read-only wrapper over the UISP NMS v2.1 HTTP API."""

    def __init__(self, api_url: str, api_token: str, timeout: float = 15.0) -> None:
        if not api_url:
            raise UispConfigurationError("UISP API URL is not configured")
        if not api_token:
            raise UispConfigurationError("UISP API token is not configured")
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> UispClient:
        timeout = float(os.getenv("UISP_TIMEOUT_SECONDS", "15"))
        return cls(
            api_url=get_uisp_api_url(),
            api_token=get_uisp_api_token(),
            timeout=timeout,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a v2.1 endpoint and return parsed JSON. Never writes."""
        if _REACHABILITY_CIRCUIT.is_open():
            raise UispClientError("UISP circuit open after recent connection failures")
        url = f"{self.api_url}{NMS_API_PREFIX}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(
                    url,
                    params=params,
                    headers={"x-auth-token": self.api_token},
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                logger.error(
                    "uisp_auth_failure",
                    extra={"event": "uisp_auth_failure", "path": path},
                )
                raise UispAuthError(
                    f"UISP API authentication failed with HTTP {exc.response.status_code}"
                ) from exc
            logger.info(
                "uisp_request_failure",
                extra={
                    "event": "uisp_request_failure",
                    "path": path,
                    "status_code": exc.response.status_code,
                },
            )
            raise UispClientError("UISP API request failed") from exc
        except (httpx.RequestError, ValueError) as exc:
            _REACHABILITY_CIRCUIT.trip()
            logger.info(
                "uisp_request_failure",
                extra={"event": "uisp_request_failure", "path": path},
            )
            raise UispClientError("UISP API request failed") from exc

        logger.info(
            "uisp_request_success",
            extra={"event": "uisp_request_success", "path": path},
        )
        return data

    def _get_list(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        data = self._get(path, params=params)
        if not isinstance(data, list):
            raise UispClientError("Invalid UISP API result")
        return [item for item in data if isinstance(item, dict)]

    def list_devices(self) -> list[dict[str, Any]]:
        """All devices known to UISP (stations, APs, ONUs, OLTs, infra)."""
        return self._get_list("/devices")

    def list_sites(self) -> list[dict[str, Any]]:
        """All sites (BTS sites + customer endpoint sites)."""
        return self._get_list("/sites")

    def list_airmax_stations(self, ap_id: str) -> list[dict[str, Any]]:
        """Stations currently associated to an airMax AP (AP-side view)."""
        return self._get_list(f"/devices/airmaxes/{ap_id}/stations")

    def list_olt_onus(self, olt_id: str) -> list[dict[str, Any]]:
        """ONUs parented under one UF-OLT (OLT-side view).

        Unlike the thin generic ``/devices`` list, the per-OLT payload carries
        a top-level ``onu`` object whose ``port`` field is the OLT-side PON
        port number — the only place UISP exposes PON-port granularity.
        """
        return self._get_list("/devices/onus", params={"parentId": olt_id})

    def list_data_links(self) -> list[dict[str, Any]]:
        """All UISP data-links — device<->device backhaul topology edges."""
        return self._get_list("/data-links")


def check_uisp_availability(timeout: float = 3.0) -> dict[str, Any]:
    """Check whether the configured UISP API accepts authenticated requests."""
    api_url = get_uisp_api_url()
    api_token = get_uisp_api_token()
    if not api_token:
        return {
            "configured": False,
            "available": False,
            "status": "not_configured",
            "api_url": api_url,
            "message": "UISP API token is not configured",
        }

    try:
        client = UispClient(api_url=api_url, api_token=api_token, timeout=timeout)
        client.list_sites()
    except UispConfigurationError as exc:
        return {
            "configured": False,
            "available": False,
            "status": "not_configured",
            "api_url": api_url,
            "message": str(exc),
        }
    except UispClientError as exc:
        return {
            "configured": True,
            "available": False,
            "status": "unavailable",
            "api_url": api_url,
            "message": str(exc),
        }

    return {
        "configured": True,
        "available": True,
        "status": "up",
        "api_url": api_url,
    }
