"""Owner-declared authentication-mechanism vocabulary and its storage mapping.

Mechanism codes are declared by the SOT domain that implements them and are
validated here. Installed verifier bindings remain database rows: a deployment
may have several bindings for one declared mechanism without changing this
registry or a migration.

This module also owns the ONE statement of how a declared mechanism is
PERSISTED, because two different vocabularies meet at the credential row and
neither may be inferred from the other:

* ``authentication_bindings.mechanism_code`` is the open, owner-declared
  MECHANISM vocabulary (ADR-0008) — ``local``, ``radius``, ``oidc``.
* ``user_credentials.provider`` (``AuthProvider``) is the coarse legacy
  STORAGE vocabulary — ``local``, ``radius``, ``sso``.

They are deliberately not the same names: a federated credential is stored as
``sso`` while its binding declares ``oidc``. Adding an ``AuthProvider.oidc``
member would create a second closed mechanism vocabulary competing with this
registry's open one, so the relationship is a declared MAPPING instead, stated
once here and consumed by everyone who needs it — the canonical projection
writer and the convergence report both read this table rather than keeping a
parallel notion of the relationship.

The mapping fails closed. A mechanism with no declared storage provider is
REFUSED: it is never defaulted and never passed through unchanged, because an
identity fallback would silently admit any mechanism whose code happens to
equal a provider value and would make the declaration decorative.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS


class UnmappedAuthenticationMechanismStorageError(ValueError):
    """A declared mechanism named no credential-storage provider.

    Raised instead of returning the mechanism code itself: an unmapped
    mechanism is a missing declaration, not a mechanism stored under its own
    name.
    """

    def __init__(self, mechanism_code: object) -> None:
        super().__init__(
            f"Authentication mechanism {str(mechanism_code)!r} declares no "
            "credential storage provider; declare one in "
            "AUTHENTICATION_MECHANISM_STORAGE."
        )
        self.mechanism_code = str(mechanism_code)


class UndeclaredAuthenticationMechanismError(ValueError):
    """A write named a mechanism no SOT domain implements."""

    def __init__(self, mechanism_code: object) -> None:
        super().__init__(
            f"Authentication mechanism {str(mechanism_code)!r} is not declared "
            "by any SOT domain."
        )
        self.mechanism_code = str(mechanism_code)


def _build_owners() -> Mapping[str, str]:
    owners: dict[str, str] = {}
    for domain_sot in DOMAIN_SOT_RELATIONSHIPS:
        for mechanism in domain_sot.authentication_mechanisms:
            owners.setdefault(mechanism, domain_sot.domain)
    return MappingProxyType(owners)


AUTHENTICATION_MECHANISM_OWNERS: Mapping[str, str] = _build_owners()


def declared_authentication_mechanisms() -> frozenset[str]:
    return frozenset(AUTHENTICATION_MECHANISM_OWNERS)


def owner_of(mechanism_code: object) -> str | None:
    return AUTHENTICATION_MECHANISM_OWNERS.get(str(mechanism_code))


def require_declared_mechanism(mechanism_code: object) -> str:
    code = str(mechanism_code).strip()
    if code not in AUTHENTICATION_MECHANISM_OWNERS:
        raise UndeclaredAuthenticationMechanismError(code)
    return code


# The single declaration of mechanism -> persisted `user_credentials.provider`
# (`AuthProvider`) value. Values are plain strings so this registry keeps no
# import on the ORM models; the architecture test proves every one of them is a
# real `AuthProvider` member and that the keys are exactly the declared
# mechanisms.
AUTHENTICATION_MECHANISM_STORAGE: Mapping[str, str] = MappingProxyType(
    {
        "local": "local",
        "radius": "radius",
        # Stored as the coarse legacy `sso` provider. The mechanism is still
        # `oidc`: what a credential row means is whatever its binding declares.
        "oidc": "sso",
    }
)


def storage_provider_for_mechanism(mechanism_code: object) -> str:
    """Return the credential provider a declared mechanism is stored as.

    Fail closed, in both directions: an undeclared mechanism raises
    ``UndeclaredAuthenticationMechanismError`` and a declared mechanism with no
    storage declaration raises ``UnmappedAuthenticationMechanismStorageError``.
    Neither is defaulted to the mechanism code itself.
    """

    code = require_declared_mechanism(mechanism_code)
    provider = AUTHENTICATION_MECHANISM_STORAGE.get(code)
    if provider is None:
        raise UnmappedAuthenticationMechanismStorageError(code)
    return provider


__all__ = [
    "AUTHENTICATION_MECHANISM_OWNERS",
    "AUTHENTICATION_MECHANISM_STORAGE",
    "UndeclaredAuthenticationMechanismError",
    "UnmappedAuthenticationMechanismStorageError",
    "declared_authentication_mechanisms",
    "owner_of",
    "require_declared_mechanism",
    "storage_provider_for_mechanism",
]
