"""Signing keys for the pinned issuer, fetched under a hard bound.

An identity provider rotates keys, so a `kid` this process has never seen is a
normal event and must be recoverable without a restart. It is also the shape of
a free amplifier: an unauthenticated caller who can post an assertion chooses
the `kid`, and a resolver that refreshes whenever it does not recognise one
turns each of those requests into an outbound request to the identity provider.
That is a denial-of-service against the IdP, mounted through Sub, at the cost
of a malformed token.

The bound has three independent parts and all three matter:

* **At most one fetch per resolution.** There is no retry loop. A failed
  refresh answers "no key" and the exchange refuses; the next caller may try
  again once the interval has passed.
* **A minimum interval between ATTEMPTS, not between successes.** The stamp
  moves before the fetch and moves whether it succeeds or fails. Stamping only
  on success would leave a permanently failing IdP being polled by every
  request, which is the amplification this exists to prevent.
* **The working key set survives a failed refresh.** A refresh that raises does
  not clear what is cached, so an IdP outage does not invalidate keys that are
  still valid.

The cache is per process and deliberately not shared through Redis: a JWKS is
small, cheap to re-fetch, and public. Sharing it would add a cross-process
write path and a cache-poisoning surface to protect a value that is already
verified by its own use — a key that does not verify the signature is simply a
key that does not verify the signature.

Nothing here logs or exports a `kid`, a URL query, or any part of a token.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.metrics import OIDC_MOBILE_JWKS_REFRESH_FAILURES
from app.services.oidc_mobile_config import OidcMobileFederationConfig

logger = logging.getLogger(__name__)

_DISCOVERY_PATH = "/.well-known/openid-configuration"

#: A JWKS is public metadata; anything larger than this is not one.
_MAX_DOCUMENT_BYTES = 512 * 1024


class JwksTransport(Protocol):
    """The one outbound seam. Replaced wholesale in tests; never partially."""

    def get_json(self, url: str, *, timeout_seconds: int) -> dict[str, Any]: ...


class _HttpxTransport:
    """The shipped transport. Kept trivial so the seam stays the only surface."""

    def get_json(self, url: str, *, timeout_seconds: int) -> dict[str, Any]:
        import httpx

        response = httpx.get(
            url,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        if len(response.content) > _MAX_DOCUMENT_BYTES:
            raise ValueError("jwks document exceeds the accepted size")
        document = response.json()
        if not isinstance(document, dict):
            raise ValueError("jwks document is not an object")
        return document


_transport: JwksTransport = _HttpxTransport()


def install_transport(transport: JwksTransport) -> JwksTransport:
    """Replace the outbound seam. Returns the previous one, for restoration."""

    global _transport
    previous = _transport
    _transport = transport
    return previous


@dataclass
class _KeySet:
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_attempt_at: float = 0.0


_lock = threading.Lock()
_cache: dict[str, _KeySet] = {}


def reset_cache() -> None:
    """Drop every cached key set. For tests and for an explicit operator reset."""

    with _lock:
        _cache.clear()


def _usable_keys(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a JWKS by `kid`, keeping only keys that could sign an assertion.

    Filtering here rather than at use is what stops a JWKS containing a
    symmetric `oct` entry from ever being reachable as a verification key: the
    algorithm allowlist already refuses HMAC, and this refuses to hold the
    material for it in the first place.
    """

    indexed: dict[str, dict[str, Any]] = {}
    entries = document.get("keys")
    if not isinstance(entries, list):
        return indexed
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("kty") != "RSA":
            continue
        use = entry.get("use")
        if use is not None and use != "sig":
            continue
        indexed[kid] = entry
    return indexed


def _jwks_uri(config: OidcMobileFederationConfig, *, timeout_seconds: int) -> str:
    if config.jwks_source == "static_uri":
        if not config.jwks_uri:
            raise ValueError("static_uri jwks source has no configured uri")
        return config.jwks_uri
    document = _transport.get_json(
        config.issuer.rstrip("/") + _DISCOVERY_PATH,
        timeout_seconds=timeout_seconds,
    )
    issuer = document.get("issuer")
    if issuer != config.issuer:
        # The discovery document must agree with the issuer we pinned. A
        # document that names a different issuer is either a misconfiguration
        # or a redirect somewhere we did not intend to trust; either way its
        # jwks_uri is not the one for our issuer.
        raise ValueError("discovery document issuer does not match the pinned issuer")
    uri = document.get("jwks_uri")
    if not isinstance(uri, str) or not uri:
        raise ValueError("discovery document declares no jwks_uri")
    return uri


def _refresh(config: OidcMobileFederationConfig, entry: _KeySet) -> None:
    """One attempt. Stamps before fetching; never clears a working key set."""

    entry.last_attempt_at = time.monotonic()
    try:
        uri = _jwks_uri(config, timeout_seconds=config.jwks_timeout_seconds)
    except Exception:
        OIDC_MOBILE_JWKS_REFRESH_FAILURES.labels(stage="discovery").inc()
        logger.warning(
            "oidc_mobile_jwks_refresh_failed",
            extra={"event": "oidc_mobile_jwks_refresh_failed", "stage": "discovery"},
        )
        return
    try:
        document = _transport.get_json(uri, timeout_seconds=config.jwks_timeout_seconds)
    except Exception:
        OIDC_MOBILE_JWKS_REFRESH_FAILURES.labels(stage="fetch").inc()
        logger.warning(
            "oidc_mobile_jwks_refresh_failed",
            extra={"event": "oidc_mobile_jwks_refresh_failed", "stage": "fetch"},
        )
        return
    keys = _usable_keys(document)
    if not keys:
        OIDC_MOBILE_JWKS_REFRESH_FAILURES.labels(stage="empty").inc()
        logger.warning(
            "oidc_mobile_jwks_refresh_failed",
            extra={"event": "oidc_mobile_jwks_refresh_failed", "stage": "empty"},
        )
        return
    entry.keys = keys


def signing_key(
    config: OidcMobileFederationConfig, kid: str | None
) -> dict[str, Any] | None:
    """The JWK for `kid`, refreshing at most once and at most that often.

    `None` is a refusal, not an error: the caller turns it into the exchange's
    `signing_key_unknown` category. A blank or absent `kid` never triggers a
    fetch — a token that does not name its key cannot be the reason to go and
    ask for keys.
    """

    if not kid:
        return None
    cache_key = f"{config.issuer}|{config.jwks_source}|{config.jwks_uri or ''}"
    with _lock:
        entry = _cache.setdefault(cache_key, _KeySet())
        found = entry.keys.get(kid)
        if found is not None:
            return found
        elapsed = time.monotonic() - entry.last_attempt_at
        if entry.last_attempt_at and elapsed < config.jwks_min_refresh_seconds:
            # Inside the bound. Refuse without contacting the identity
            # provider: this is the branch that makes an unknown-`kid` flood
            # cost nothing outbound.
            return None
        _refresh(config, entry)
        return entry.keys.get(kid)
