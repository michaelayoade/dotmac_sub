"""OLT REST API client for vendor-specific REST interfaces.

This module provides HTTP client functionality for OLTs that expose REST APIs
(in addition to or instead of SSH/NETCONF). It follows the same patterns as
genieacs.py for HTTP communication.

Supports:
- Basic Auth (username + password)
- Bearer Token (API token)
- Unauthenticated access (for development/testing)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.services.credential_crypto import decrypt_credential

if TYPE_CHECKING:
    from app.models.network import OLTDevice

logger = logging.getLogger(__name__)


class OltRestError(Exception):
    """Base exception for OLT REST API errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OltRestConnectionError(OltRestError):
    """Raised when connection to OLT REST API fails."""

    pass


class OltRestAuthenticationError(OltRestError):
    """Raised when authentication to OLT REST API fails."""

    pass


class OltRestClient:
    """HTTP client for OLT REST APIs.

    Supports Basic Auth, Bearer Token, and unauthenticated access.
    Authentication type is determined by the OLT's api_auth_type field.

    Usage:
        from app.services.network.olt_rest_client import OltRestClient

        client = OltRestClient(olt)
        response = client.get("/api/v1/onts")
        data = response.json()
    """

    def __init__(self, olt: OLTDevice, timeout: float = 30.0):
        """Initialize OLT REST client.

        Args:
            olt: OLT device with REST API configuration.
            timeout: Request timeout in seconds.

        Raises:
            ValueError: If OLT is not configured for REST API.
        """
        self._olt = olt
        self._timeout = timeout
        self._base_url = self._build_base_url()

    def _build_base_url(self) -> str:
        """Build the base URL for API requests.

        Returns:
            Base URL string.

        Raises:
            ValueError: If no URL or management IP is configured.
        """
        # Use api_url if configured, otherwise construct from mgmt_ip
        if self._olt.api_url:
            return self._olt.api_url.rstrip("/")

        if not self._olt.mgmt_ip:
            raise ValueError(f"OLT {self._olt.name} has no api_url or mgmt_ip configured")

        # Construct URL from mgmt_ip and api_port
        port = self._olt.api_port or 443
        scheme = "https" if port == 443 else "http"
        return f"{scheme}://{self._olt.mgmt_ip}:{port}"

    def _build_auth_headers(self) -> dict[str, str]:
        """Build authentication headers based on OLT configuration.

        Returns:
            Dictionary of headers for authentication.
        """
        headers: dict[str, str] = {}
        auth_type = (self._olt.api_auth_type or "").lower()

        if auth_type == "bearer":
            token = self._olt.api_token
            if token:
                # Decrypt if encrypted
                decrypted = decrypt_credential(token)
                headers["Authorization"] = f"Bearer {decrypted}"
        # Basic auth is handled via httpx auth parameter, not headers

        return headers

    def _get_basic_auth(self) -> httpx.BasicAuth | None:
        """Get Basic Auth credentials if configured.

        Returns:
            BasicAuth instance or None.
        """
        auth_type = (self._olt.api_auth_type or "").lower()

        if auth_type == "basic":
            username = self._olt.api_username
            password = self._olt.api_password
            if username and password:
                # Decrypt password if encrypted
                decrypted_password = decrypt_credential(password)
                if decrypted_password:
                    return httpx.BasicAuth(username, decrypted_password)

        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make HTTP request to OLT REST API.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE, etc.).
            path: API path (will be appended to base URL).
            params: Query parameters.
            json_data: JSON request body.
            headers: Additional headers.
            **kwargs: Additional httpx arguments.

        Returns:
            HTTP response.

        Raises:
            OltRestError: On request failure.
            OltRestConnectionError: On connection failure.
            OltRestAuthenticationError: On authentication failure (401/403).
        """
        url = f"{self._base_url}{path}"

        # Merge headers
        request_headers = self._build_auth_headers()
        if headers:
            request_headers.update(headers)

        # Get auth
        auth = self._get_basic_auth()

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                    headers=request_headers,
                    auth=auth,
                    **kwargs,
                )

                # Check for auth errors
                if response.status_code in (401, 403):
                    logger.warning(
                        "OLT REST API authentication failed: olt=%s status=%d",
                        self._olt.name,
                        response.status_code,
                    )
                    raise OltRestAuthenticationError(
                        f"Authentication failed: {response.status_code}",
                        status_code=response.status_code,
                    )

                response.raise_for_status()
                return response

        except httpx.ConnectError as exc:
            logger.error(
                "OLT REST API connection failed: olt=%s url=%s error=%s",
                self._olt.name,
                url,
                exc,
            )
            raise OltRestConnectionError(f"Connection failed: {exc}") from exc

        except httpx.TimeoutException as exc:
            logger.error(
                "OLT REST API timeout: olt=%s url=%s",
                self._olt.name,
                url,
            )
            raise OltRestConnectionError(f"Request timeout: {exc}") from exc

        except httpx.HTTPStatusError as exc:
            logger.error(
                "OLT REST API error: olt=%s status=%d body=%s",
                self._olt.name,
                exc.response.status_code,
                exc.response.text[:500],
            )
            raise OltRestError(
                f"API error: {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc

        except httpx.RequestError as exc:
            logger.error(
                "OLT REST API request error: olt=%s error=%s",
                self._olt.name,
                exc,
            )
            raise OltRestError(f"Request error: {exc}") from exc

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make GET request to OLT REST API.

        Args:
            path: API path.
            params: Query parameters.
            **kwargs: Additional httpx arguments.

        Returns:
            HTTP response.
        """
        return self.request("GET", path, params=params, **kwargs)

    def post(
        self,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make POST request to OLT REST API.

        Args:
            path: API path.
            json_data: JSON request body.
            **kwargs: Additional httpx arguments.

        Returns:
            HTTP response.
        """
        return self.request("POST", path, json_data=json_data, **kwargs)

    def put(
        self,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make PUT request to OLT REST API.

        Args:
            path: API path.
            json_data: JSON request body.
            **kwargs: Additional httpx arguments.

        Returns:
            HTTP response.
        """
        return self.request("PUT", path, json_data=json_data, **kwargs)

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make DELETE request to OLT REST API.

        Args:
            path: API path.
            params: Query parameters.
            **kwargs: Additional httpx arguments.

        Returns:
            HTTP response.
        """
        return self.request("DELETE", path, params=params, **kwargs)

    def test_connection(self) -> tuple[bool, str]:
        """Test connectivity to OLT REST API.

        Makes a simple request to verify the API is reachable and
        authentication is working.

        Returns:
            Tuple of (success, message).
        """
        try:
            # Try a simple GET request to root or health endpoint
            # Many APIs have a /health or / endpoint for this purpose
            response = self.get("/")
            return True, f"Connected successfully (status {response.status_code})"
        except OltRestAuthenticationError as exc:
            return False, f"Authentication failed: {exc}"
        except OltRestConnectionError as exc:
            return False, f"Connection failed: {exc}"
        except OltRestError as exc:
            return False, f"API error: {exc}"


def get_rest_client(olt: OLTDevice, timeout: float = 30.0) -> OltRestClient:
    """Get REST client for an OLT.

    Args:
        olt: OLT device with REST API configuration.
        timeout: Request timeout in seconds.

    Returns:
        OltRestClient instance.

    Raises:
        ValueError: If OLT is not configured for REST API.
    """
    if not olt.api_enabled:
        raise ValueError(f"REST API not enabled on OLT {olt.name}")

    return OltRestClient(olt, timeout=timeout)
