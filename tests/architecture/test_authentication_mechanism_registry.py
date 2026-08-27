"""Authentication mechanisms are open declarations, not a host enum."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.models.auth import AuthProvider
from app.services.authentication_mechanism_registry import (
    UndeclaredAuthenticationMechanismError,
    declared_authentication_mechanisms,
    owner_of,
    require_declared_mechanism,
)
from app.services.sot_registry import registry as registry_module
from app.services.sot_registry.registry import (
    authentication_mechanism_declaration_errors,
    domain_relationship,
)


def test_only_implemented_mechanisms_are_declared_by_one_owner() -> None:
    assert declared_authentication_mechanisms() == {"local", "oidc", "radius"}
    assert owner_of("local") == "authorization_control_plane"
    assert owner_of("radius") == "network_access_control_plane"
    # `oidc` is implemented by app.services.oidc_mobile_federation, which is
    # registered in that same domain. It is declared because there IS a
    # verifier, not because the word exists.
    assert owner_of("oidc") == "authorization_control_plane"
    # The legacy persisted column value stays undeclared. `sso` is the coarse
    # `AuthProvider` member a federated credential is stored as; `oidc` is the
    # declared MECHANISM its authentication_binding carries. Declaring the
    # column value too would give one mechanism two names and let a write name
    # the vaguer one.
    assert "sso" in {item.value for item in AuthProvider}
    assert owner_of("sso") is None
    with pytest.raises(UndeclaredAuthenticationMechanismError):
        require_declared_mechanism("sso")


def test_the_declared_oidc_mechanism_has_a_real_consumer() -> None:
    """A declaration with no verifier behind it is an orphan.

    The registry cannot tell the difference between "we implement this" and
    "someone added a string", so this names the consumer: the federation owner
    resolves its verifier binding through `require_declared_mechanism`, which
    means removing the implementation makes the declaration fail rather than
    quietly become decorative.
    """

    from app.services.oidc_mobile_config import OIDC_MECHANISM_CODE

    assert require_declared_mechanism(OIDC_MECHANISM_CODE) == "oidc"

    source = (
        Path(__file__).resolve().parents[2]
        / "app"
        / "services"
        / "oidc_mobile_federation.py"
    ).read_text(encoding="utf-8")
    assert "require_declared_mechanism(OIDC_MECHANISM_CODE)" in source


def test_duplicate_mechanism_declaration_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    party = domain_relationship("party_identity")
    monkeypatch.setattr(
        registry_module,
        "DOMAIN_SOT_RELATIONSHIPS",
        (
            *registry_module.DOMAIN_SOT_RELATIONSHIPS,
            replace(party, authentication_mechanisms=("local",)),
        ),
    )

    errors = authentication_mechanism_declaration_errors()

    assert any("local" in error and "both" in error for error in errors)
