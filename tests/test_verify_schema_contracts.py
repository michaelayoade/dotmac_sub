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

    with pytest.raises(RuntimeError, match="public.unfinished_index"):
        verification.verify_schema_contracts(bind)
