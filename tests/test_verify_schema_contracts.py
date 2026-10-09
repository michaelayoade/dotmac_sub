from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.migration import verify_schema_contracts as verification
from scripts.migration.regional_report_billing_indexes import (
    INDEX_SPECS,
    RegionalReportIndexContractError,
)


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
        verification, "validate_regional_report_indexes", lambda _bind: None
    )
    monkeypatch.setattr(
        verification,
        "invalid_postgres_indexes",
        lambda _bind: (("public", "radius_accounting_sessions", "unfinished_index"),),
    )

    with pytest.raises(RuntimeError, match="public.unfinished_index"):
        verification.verify_schema_contracts(bind)


def test_report_contract_failure_blocks_service_replacement(monkeypatch) -> None:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(verification, "validate_postgres_index", lambda _bind: None)

    def invalid_report_index(_bind) -> None:
        raise RegionalReportIndexContractError(
            spec=INDEX_SPECS[0], issues=("index is not valid",)
        )

    monkeypatch.setattr(
        verification, "validate_regional_report_indexes", invalid_report_index
    )
    with pytest.raises(RegionalReportIndexContractError, match="is not valid"):
        verification.verify_schema_contracts(bind)
