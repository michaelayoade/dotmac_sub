"""Trusted federation configuration, resolved from deployment state alone.

The one rule this module exists to make structural: **configuration is
selected from the deployment, never from the caller**. Nothing here reads a
request body, a header, a token claim, or a value the device sent. An attacker
who fully controls both the ceremony request and the assertion still cannot
change which issuer is trusted, which client id is expected, which redirect URI
is bound, or which installed verifier answers — because none of those arrive
from the wire.

Every identifier is declared `inherits=False` (see
``app.services.settings_spec``): a platform row must not stand in for a missing
tenant row, because a less-specific answer to "which issuer" names the wrong
identity rather than a weaker one.

Required-ness is CONDITIONAL and enforced here rather than by the kernel's
unconditional ``required_at``. ``require_federation_config`` refuses to build a
configuration when any identifier is missing, and ``app.main._startup_preflight``
calls it whenever the control is on — so a deployment that has enabled the
mechanism fails loudly at boot, and one that has not is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import control_registry
from app.services.domain_errors import DomainError
from app.services.settings_spec import resolve_integer, resolve_value

#: The control that gates the whole mechanism. Declared in
#: ``app.services.control_registry``; default OFF, fail closed.
OIDC_MOBILE_CONTROL = "auth.oidc_mobile_federation"

#: The declared authentication mechanism code these bindings carry.
OIDC_MECHANISM_CODE = "oidc"

#: The ONE PKCE challenge method a ceremony may declare. Not a setting: `plain`
#: is not a weaker configuration of the same thing, it is the absence of the
#: protection, and a knob that can switch it off is a knob that will be found
#: switched off.
REQUIRED_CODE_CHALLENGE_METHOD = "S256"

#: The ID token signing algorithms Sub accepts. Not a setting either, and the
#: reason is the same shape: `none` and the HMAC family are not weaker
#: configurations, they are the removal of the signature check (`none`) or the
#: substitution of a symmetric key an attacker may already hold (`HS*`). A
#: configurable algorithm list is how "alg: none" becomes reachable in
#: production. Widening this is a code change and a review.
ALLOWED_ID_TOKEN_ALGORITHMS = frozenset({"RS256"})

#: Every identifier a ceremony pins. Missing any one of them is a refusal, not
#: a default.
_REQUIRED_KEYS = (
    "oidc_mobile_issuer",
    "oidc_mobile_client_id",
    "oidc_mobile_redirect_uri",
    "oidc_mobile_audience",
    "oidc_mobile_binding_key",
    "oidc_mobile_deployment_id",
)


class OidcFederationConfigError(DomainError):
    """The deployment has not configured the federation it enabled."""


@dataclass(frozen=True, slots=True)
class OidcMobileFederationConfig:
    """One deployment's complete, trusted federation configuration."""

    issuer: str
    client_id: str
    redirect_uri: str
    audience: str
    binding_key: str
    deployment_id: str
    jwks_source: str
    jwks_uri: str | None
    jwks_min_refresh_seconds: int
    jwks_timeout_seconds: int
    ceremony_ttl_seconds: int
    clock_skew_seconds: int
    max_assertion_age_seconds: int


def federation_enabled(db: Session) -> bool:
    """Whether this deployment has turned the mechanism on. Fails closed."""

    return control_registry.is_enabled(db, OIDC_MOBILE_CONTROL)


def _text(db: Session, key: str) -> str | None:
    value = resolve_value(db, SettingDomain.auth, key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def require_federation_config(db: Session) -> OidcMobileFederationConfig:
    """The complete configuration, or a refusal naming every missing key.

    Reporting all of them at once is deliberate: an operator bringing the
    mechanism up should see every unset identifier in one pass rather than
    rediscover them one restart at a time.
    """

    resolved = {key: _text(db, key) for key in _REQUIRED_KEYS}
    missing = sorted(key for key, value in resolved.items() if value is None)

    jwks_source = _text(db, "oidc_mobile_jwks_source") or "discovery"
    jwks_uri = _text(db, "oidc_mobile_jwks_uri")
    if jwks_source == "static_uri" and jwks_uri is None:
        # `static_uri` names a URI. Falling back to discovery here would make a
        # misconfigured deployment quietly contact a different endpoint from
        # the one it declared.
        missing.append("oidc_mobile_jwks_uri")

    if missing:
        raise OidcFederationConfigError(
            code="auth.oidc_mobile_federation.configuration_incomplete",
            message=(
                "Field-mobile OIDC federation is enabled but not configured. "
                "Set every listed auth setting, then restart."
            ),
            details={"missing_settings": sorted(missing)},
        )

    return OidcMobileFederationConfig(
        issuer=str(resolved["oidc_mobile_issuer"]),
        client_id=str(resolved["oidc_mobile_client_id"]),
        redirect_uri=str(resolved["oidc_mobile_redirect_uri"]),
        audience=str(resolved["oidc_mobile_audience"]),
        binding_key=str(resolved["oidc_mobile_binding_key"]),
        deployment_id=str(resolved["oidc_mobile_deployment_id"]),
        jwks_source=jwks_source,
        jwks_uri=jwks_uri,
        jwks_min_refresh_seconds=resolve_integer(
            db, SettingDomain.auth, "oidc_mobile_jwks_min_refresh_seconds"
        ),
        jwks_timeout_seconds=resolve_integer(
            db, SettingDomain.auth, "oidc_mobile_jwks_timeout_seconds"
        ),
        ceremony_ttl_seconds=resolve_integer(
            db, SettingDomain.auth, "oidc_mobile_ceremony_ttl_seconds"
        ),
        clock_skew_seconds=resolve_integer(
            db, SettingDomain.auth, "oidc_mobile_clock_skew_seconds"
        ),
        max_assertion_age_seconds=resolve_integer(
            db, SettingDomain.auth, "oidc_mobile_max_assertion_age_seconds"
        ),
    )


def verify_startup_configuration(db: Session) -> None:
    """Boot gate: an enabled mechanism must be completely configured.

    A no-op when the control is off, which is the shipped state. When it is on,
    a missing identifier raises and the process refuses to start — the
    alternative is serving ceremonies that point at nothing.
    """

    if not federation_enabled(db):
        return
    require_federation_config(db)
