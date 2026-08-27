"""The installed ID-token verifier: one long-lived object per registration.

Sub no longer implements ID-token verification. `dotmac-auth-oidc` owns the
whole security core — signature against the pinned issuer's key set, the
algorithm allowlist applied BEFORE key resolution, the JWK's declared `alg`
against the token's, exact `iss`, `aud`/`azp`, `exp`/`nbf`/`iat` with leeway,
the maximum token age in both directions, and the constant-time nonce
comparison. This module is the only thing Sub keeps: the translation from
Sub's own deployment settings into that package's registration, and the
process-lifetime cache that makes the translation affordable.

## Why the verifier is held rather than built

`NativeIDTokenVerifier` owns a `ProviderCache`, and that cache is the bound on
outbound requests to the identity provider. A verifier constructed per request
would have an empty cache every time, so every exchange would fetch the key
set — turning an unauthenticated endpoint into an amplifier pointed at the
IdP, which is the exact failure the bound exists to prevent. So a verifier is
built ONCE per registration and retained for the process lifetime.

A "registration" is the complete tuple of trusted values a verification
depends on. Keying the cache on that tuple rather than on a bare issuer is
what makes a settings change take effect: an operator who repoints
`oidc_mobile_jwks_uri` gets a new verifier with a new cache, and the old one
is dropped rather than left answering with keys from the previous endpoint.

## No outbound call lives here

The package's `Transport` is the only thing that touches a network, and Sub
never implements one in production — `HttpxTransport` inside the package is
the shipped default. `install_transport` exists so a test can inject a
counting fake and falsify the bound; it is not a production seam and there is
no setting that reaches it.

Nothing here logs, and there is nothing here to log: this module holds a
configuration and a cache, never a token, a nonce or a subject.
"""

from __future__ import annotations

import threading

from dotmac_auth_oidc.native import (
    NativeIDTokenVerifier,
    PublicNativeClientConfig,
)
from dotmac_auth_oidc.transport import Transport

from app.services.oidc_mobile_config import (
    JWKS_SOURCE_STATIC_URI,
    OidcMobileFederationConfig,
)

#: The identity of one registration. Every value a verification depends on is
#: in here, so a change to any of them yields a DIFFERENT verifier with its own
#: cache rather than a stale one answering under new settings.
_RegistrationKey = tuple[str, str, str | None, int, int, float, float]

_lock = threading.Lock()
_verifiers: dict[_RegistrationKey, NativeIDTokenVerifier] = {}
_transport: Transport | None = None


def native_client_config(
    config: OidcMobileFederationConfig,
) -> PublicNativeClientConfig:
    """Sub's deployment settings as the package's public-native registration.

    The two JWKS sources map onto the package's two MUTUALLY EXCLUSIVE
    overrides, and neither is guessed: `discovery` leaves both unset so the
    package derives the well-known URL from the pinned issuer, and
    `static_uri` sets `jwks_uri` alone. `require_federation_config` refuses the
    combination of a discovery source with a configured URI, so there is no
    case here where one silently wins over the other.
    """

    return PublicNativeClientConfig(
        issuer=config.issuer,
        # The package verifies `aud` against the client id, and
        # `require_federation_config` already refuses a deployment whose
        # declared audience is anything else.
        client_id=config.client_id,
        max_token_age_seconds=config.max_assertion_age_seconds,
        leeway_seconds=config.clock_skew_seconds,
        jwks_uri=(
            config.jwks_uri if config.jwks_source == JWKS_SOURCE_STATIC_URI else None
        ),
    )


def _key(registration: PublicNativeClientConfig, config) -> _RegistrationKey:
    return (
        registration.issuer,
        registration.client_id,
        registration.jwks_uri,
        registration.max_token_age_seconds,
        registration.leeway_seconds,
        float(config.jwks_timeout_seconds),
        float(config.jwks_min_refresh_seconds),
    )


def get_verifier(config: OidcMobileFederationConfig) -> NativeIDTokenVerifier:
    """The verifier for this registration, built at most once per process.

    Sub's own knobs reach the package here and nowhere else:
    `oidc_mobile_jwks_timeout_seconds` becomes the transport timeout, and
    `oidc_mobile_jwks_min_refresh_seconds` becomes the floor between two forced
    key-set refetches — the amplification bound the setting was named for.
    """

    registration = native_client_config(config)
    key = _key(registration, config)
    with _lock:
        held = _verifiers.get(key)
        if held is not None:
            return held
        built = NativeIDTokenVerifier(
            registration,
            transport=_transport,
            timeout=float(config.jwks_timeout_seconds),
            jwks_min_refetch=float(config.jwks_min_refresh_seconds),
        )
        _verifiers[key] = built
        return built


def install_transport(transport: Transport | None) -> Transport | None:
    """Replace the transport newly built verifiers are given. Tests only.

    Returns the previous one so a test can restore it. Held verifiers keep the
    transport they were built with, so a caller that installs one must also
    `reset_verifiers()` — which is why both are exported together.
    """

    global _transport
    with _lock:
        previous = _transport
        _transport = transport
        return previous


def reset_verifiers() -> None:
    """Drop every held verifier, and with it every cached key set."""

    with _lock:
        _verifiers.clear()


__all__ = [
    "get_verifier",
    "install_transport",
    "native_client_config",
    "reset_verifiers",
]
