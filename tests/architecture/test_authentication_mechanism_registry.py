"""Authentication mechanisms are open declarations, not a host enum."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from app.models.auth import AuthProvider
from app.services import authentication_mechanism_registry as mechanism_registry
from app.services.authentication_mechanism_registry import (
    AUTHENTICATION_MECHANISM_STORAGE,
    UndeclaredAuthenticationMechanismError,
    UnmappedAuthenticationMechanismStorageError,
    declared_authentication_mechanisms,
    owner_of,
    require_declared_mechanism,
    storage_provider_for_mechanism,
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


def test_the_registry_owns_the_one_mechanism_to_storage_mapping() -> None:
    """Two vocabularies, one declaration of how they relate.

    `mechanism_code` is the open owner-declared MECHANISM vocabulary;
    `AuthProvider` is the coarse persisted STORAGE vocabulary. They are not the
    same names — `oidc` is stored as `sso` — so the relationship is declared
    here rather than inferred by comparing the two strings.
    """

    assert dict(AUTHENTICATION_MECHANISM_STORAGE) == {
        "local": "local",
        "radius": "radius",
        "oidc": "sso",
    }
    providers = {item.value for item in AuthProvider}
    assert set(AUTHENTICATION_MECHANISM_STORAGE.values()) <= providers
    # The second vocabulary that must never exist: an `AuthProvider.oidc`
    # member would make the enum a competing mechanism vocabulary, and a write
    # could then name the mechanism in the storage column.
    assert "oidc" not in providers


def test_every_declared_mechanism_declares_exactly_one_storage_provider() -> None:
    """A mechanism nobody mapped is unusable, not silently identity-mapped.

    Both directions: a declared mechanism with no storage declaration cannot be
    provisioned, and a storage declaration for an undeclared mechanism is a
    mapping with no owner behind it.
    """

    assert set(AUTHENTICATION_MECHANISM_STORAGE) == declared_authentication_mechanisms()
    for mechanism in declared_authentication_mechanisms():
        assert (
            storage_provider_for_mechanism(mechanism)
            == (AUTHENTICATION_MECHANISM_STORAGE[mechanism])
        )


def test_an_unmapped_mechanism_is_refused_rather_than_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sensitivity proof for the mapping itself.

    The bug this replaced was an implicit identity mapping: the code compared
    `mechanism_code` with the provider directly, so an unmapped mechanism whose
    code happened to equal a provider value would have been accepted. Removing
    a mapping must therefore RAISE, never return the mechanism code.
    """

    with pytest.raises(UndeclaredAuthenticationMechanismError):
        storage_provider_for_mechanism("saml")

    monkeypatch.setattr(
        mechanism_registry,
        "AUTHENTICATION_MECHANISM_STORAGE",
        MappingProxyType({"local": "local", "radius": "radius"}),
    )

    with pytest.raises(UnmappedAuthenticationMechanismStorageError) as raised:
        storage_provider_for_mechanism("oidc")

    assert raised.value.mechanism_code == "oidc"
    # There is deliberately no value to compare against: the old shape would
    # have answered "oidc" here by implication, and refusing instead of
    # answering is exactly the property under test.
