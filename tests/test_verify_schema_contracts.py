from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.migration import verify_schema_contracts as verification


def test_non_postgres_database_is_not_subject_to_catalog_contracts(monkeypatch) -> None:
    monkeypatch.setattr(
        verification,
        "validate_postgres_index",
        lambda _bind: pytest.fail("Postgres validation must not run"),
    )
    monkeypatch.setattr(
        verification,
        "invalid_enabled_manifest_pins",
        lambda _bind: pytest.fail("Manifest validation must not run"),
    )
    bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    verification.verify_schema_contracts(bind)


def test_any_invalid_user_index_blocks_service_replacement(monkeypatch) -> None:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(verification, "validate_postgres_index", lambda _bind: None)
    monkeypatch.setattr(
        verification,
        "invalid_postgres_indexes",
        lambda _bind: (("public", "radius_accounting_sessions", "unfinished_index"),),
    )
    monkeypatch.setattr(
        verification,
        "invalid_enabled_manifest_pins",
        lambda _bind: (),
    )

    with pytest.raises(RuntimeError, match="public.unfinished_index"):
        verification.verify_schema_contracts(bind)


def test_enabled_manifest_drift_blocks_service_replacement(monkeypatch) -> None:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(verification, "validate_postgres_index", lambda _bind: None)
    monkeypatch.setattr(verification, "invalid_postgres_indexes", lambda _bind: ())
    monkeypatch.setattr(
        verification,
        "invalid_enabled_manifest_pins",
        lambda _bind: (
            verification.InvalidManifestPin(
                installation_name="Paystack production",
                connector_key="paystack",
                installed_version="1.0.0",
                deployed_version="1.0.0",
                installed_digest="a" * 64,
                deployed_digest="b" * 64,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="explicit adoption migration"):
        verification.verify_schema_contracts(bind)


def test_matching_enabled_manifest_pins_pass_deployment_gate(monkeypatch) -> None:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(verification, "validate_postgres_index", lambda _bind: None)
    monkeypatch.setattr(verification, "invalid_postgres_indexes", lambda _bind: ())
    monkeypatch.setattr(
        verification,
        "invalid_enabled_manifest_pins",
        lambda _bind: (),
    )

    verification.verify_schema_contracts(bind)
