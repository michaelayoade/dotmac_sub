"""Trust-anchor requirements for final prepaid reconstruction."""

import pytest

from app.models.domain_settings import SettingDomain
from app.services import prepaid_funding_attestation, settings_spec


def test_trust_anchor_rejects_plaintext_or_environment_fallback(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: "env://UNTRUSTED_PUBLIC_KEY",
    )

    with pytest.raises(ValueError, match="must be an OpenBao reference"):
        prepaid_funding_attestation.resolve_trusted_public_key_pem(db_session)


def test_trust_anchor_resolves_only_the_config_owned_openbao_reference(
    db_session, monkeypatch
):
    reference = "bao://secret/billing/prepaid-reconstruction-attestation#public_key_pem"
    resolved_refs: list[str] = []
    monkeypatch.setattr(
        settings_spec,
        "resolve_value",
        lambda _db, domain, key: (
            reference
            if domain == SettingDomain.billing
            and key == prepaid_funding_attestation.TRUST_KEY_SETTING
            else None
        ),
    )
    monkeypatch.setattr(
        prepaid_funding_attestation,
        "resolve_secret",
        lambda value: resolved_refs.append(value) or "configured-public-key",
    )

    assert (
        prepaid_funding_attestation.resolve_trusted_public_key_pem(db_session)
        == "configured-public-key"
    )
    assert resolved_refs == [reference]
