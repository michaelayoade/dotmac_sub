"""Flutterwave v4 authentication and webhook verification.

Flutterwave withdrew v3 API key issuance for the Dotmac account, and v4 differs
from v3 in ways that are not a URL swap:

- **Auth.** v3 used a static ``FLWSECK_`` bearer key. v4 uses OAuth 2.0 client
  credentials against a Keycloak IDP, returning tokens that live 10 minutes. A
  connector therefore needs a token lifecycle, not a stored bearer.
- **Webhooks.** v3 sent the shared secret verbatim in ``verif-hash`` and the
  receiver compared for equality. v4 sends ``flutterwave-signature``, an
  HMAC-SHA256 of the raw body keyed with the secret hash, base64 encoded.
  Comparing a v4 signature the v3 way rejects every webhook.
- **Public key.** v4 has no public key. OAuth replaces the publishable/secret
  key pair, so a v4 installation must not be required to carry one.

This module holds the two pieces that are precisely specified and verifiable
without live credentials. The v4 resource endpoints (charge creation via the
orchestrator flow, retrieval, refunds) are deliberately not implemented here
from documentation alone: v4 is in public beta and its published docs disagree
with each other, so those are built against the sandbox with real responses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

TOKEN_ENDPOINT = (
    "https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token"
)
WEBHOOK_SIGNATURE_HEADER = "flutterwave-signature"
# Flutterwave issues 10-minute tokens and advises refreshing at least a minute
# before expiry, so a request never races the boundary.
_REFRESH_MARGIN_SECONDS = 60
_DEFAULT_TOKEN_LIFETIME_SECONDS = 600
_TOKEN_REQUEST_TIMEOUT_SECONDS = 15.0


class FlutterwaveAuthError(RuntimeError):
    """The IDP would not issue a token. Never carries the client secret."""


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: float

    def usable(self, *, now: float) -> bool:
        return bool(self.value) and now < self.expires_at


class FlutterwaveTokenManager:
    """Acquire, cache and refresh v4 access tokens for one client.

    Tokens are cached in process and reused until shortly before expiry, so a
    burst of operations costs one token exchange rather than one per call —
    which also keeps us clear of IDP rate limits.

    The cache key is the client id only; the secret is never used as a key, so
    it cannot leak through a repr, a log line, or a crash dump of the cache.
    """

    def __init__(
        self,
        *,
        token_endpoint: str = TOKEN_ENDPOINT,
        client_override: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token_endpoint = token_endpoint
        self._client_override = client_override
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, _CachedToken] = {}

    def token(self, *, client_id: str, client_secret: str) -> str:
        """Return a usable bearer token, fetching one only when needed."""
        if not client_id or not client_secret:
            raise FlutterwaveAuthError("client id and client secret are required")

        now = self._clock()
        with self._lock:
            cached = self._tokens.get(client_id)
            if cached is not None and cached.usable(now=now):
                return cached.value

        fetched = self._fetch(client_id=client_id, client_secret=client_secret)
        with self._lock:
            self._tokens[client_id] = fetched
        return fetched.value

    def invalidate(self, *, client_id: str) -> None:
        """Drop a cached token, e.g. after the provider rejects it as expired."""
        with self._lock:
            self._tokens.pop(client_id, None)

    def _fetch(self, *, client_id: str, client_secret: str) -> _CachedToken:
        client = self._client_override or httpx.Client()
        owned = self._client_override is None
        try:
            response = client.post(
                self._token_endpoint,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_TOKEN_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            # Deliberately not interpolating the exception's request, which can
            # echo the posted form and therefore the client secret.
            raise FlutterwaveAuthError(
                "could not reach the Flutterwave identity provider"
            ) from None
        finally:
            if owned:
                client.close()

        if response.status_code != 200:
            raise FlutterwaveAuthError(
                f"identity provider rejected the client credentials "
                f"(HTTP {response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError:
            raise FlutterwaveAuthError(
                "identity provider response was not JSON"
            ) from None

        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise FlutterwaveAuthError("identity provider returned no access token")

        try:
            lifetime = int(payload.get("expires_in") or _DEFAULT_TOKEN_LIFETIME_SECONDS)
        except (TypeError, ValueError):
            lifetime = _DEFAULT_TOKEN_LIFETIME_SECONDS
        # Refresh early; a token that expires mid-flight would surface as an
        # opaque 401 on a payment call.
        usable_for = max(1, lifetime - _REFRESH_MARGIN_SECONDS)
        return _CachedToken(value=access_token, expires_at=self._clock() + usable_for)


def expected_webhook_signature(*, secret_hash: str, body: bytes) -> str:
    """The v4 signature Flutterwave should have sent for this body.

    HMAC-SHA256 over the raw body, keyed with the dashboard secret hash, base64
    encoded. The raw bytes matter: re-serialising the JSON changes whitespace
    and key order and would produce a different digest.
    """
    digest = hmac.new(secret_hash.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_webhook_signature(*, secret_hash: str, body: bytes, signature: str) -> bool:
    """Whether a v4 webhook genuinely came from Flutterwave.

    Fails closed on a missing secret or signature rather than treating absence
    as a match, and compares in constant time.
    """
    if not secret_hash or not signature:
        return False
    expected = expected_webhook_signature(secret_hash=secret_hash, body=body)
    return hmac.compare_digest(expected, signature)


__all__ = [
    "TOKEN_ENDPOINT",
    "WEBHOOK_SIGNATURE_HEADER",
    "FlutterwaveAuthError",
    "FlutterwaveTokenManager",
    "expected_webhook_signature",
    "verify_webhook_signature",
]
