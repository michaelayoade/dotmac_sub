"""Fast unit contracts only; PostgreSQL migration proof lives in integration/."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from scripts.migration import regional_report_billing_indexes as contract

INVOICE, PAYMENT = contract.INDEX_SPECS


def _state(spec: contract.IndexSpec = INVOICE) -> contract.IndexState:
    return contract.IndexState(
        relation_kind="i",
        table_schema="public",
        table_name=spec.table,
        valid=True,
        ready=True,
        live=True,
        unique=False,
        constraint_backed=False,
        access_method="btree",
        key_attribute_count=4,
        total_attribute_count=4,
        has_predicate=False,
        has_expressions=False,
        keys=spec.keys,
        descending=(False,) * 4,
        nulls_first=(False,) * 4,
        building=False,
    )


@pytest.fixture
def bind() -> Iterator[sa.engine.Connection]:
    # No schema claims: catalog reads are replaced by typed observations.
    engine = sa.create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


@pytest.mark.parametrize("spec", contract.INDEX_SPECS)
@pytest.mark.parametrize("initial", ("missing", "invalid", "unready", "valid"))
def test_catalog_state_controls_safe_retry(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
    spec: contract.IndexSpec,
    initial: str,
) -> None:
    valid = _state(spec)
    state = (
        None
        if initial == "missing"
        else replace(
            valid,
            valid=initial != "invalid",
            ready=initial != "unready",
        )
    )
    observations = iter((state, valid))
    monkeypatch.setattr(
        contract, "postgres_index_state", lambda _bind, **_kwargs: next(observations)
    )
    statements: list[str] = []

    outcome = contract.ensure_postgres_index(bind, statements.append, spec=spec)

    if initial == "valid":
        assert outcome is contract.RepairOutcome.unchanged
        assert statements == []
    elif initial == "missing":
        assert outcome is contract.RepairOutcome.created
        assert statements == [spec.create_sql]
    else:
        assert outcome is contract.RepairOutcome.rebuilt
        assert statements == [spec.drop_sql, spec.create_sql]
    assert "IF NOT EXISTS" not in spec.create_sql


MALFORMED_STATES = (
    replace(_state(), relation_kind="r"),
    replace(_state(), table_schema="another_schema"),
    replace(_state(), table_name="payments"),
    replace(_state(), unique=True),
    replace(_state(), constraint_backed=True),
    replace(_state(), live=False),
    replace(_state(), building=True),
    replace(_state(), access_method="hash"),
    replace(_state(), key_attribute_count=3),
    replace(_state(), total_attribute_count=5),
    replace(_state(), has_predicate=True),
    replace(_state(), has_expressions=True),
    replace(_state(), keys=("status", "is_active", "issued_at", "account_id")),
    replace(
        _state(), keys=("is_active", 'status COLLATE "C"', "issued_at", "account_id")
    ),
    replace(
        _state(),
        keys=("is_active", "status text_pattern_ops", "issued_at", "account_id"),
    ),
    replace(_state(), descending=(False, True, False, False)),
    replace(_state(), nulls_first=(False, True, False, False)),
)


@pytest.mark.parametrize("malformed", MALFORMED_STATES)
@pytest.mark.parametrize("valid", (True, False))
def test_unexpected_objects_are_never_dropped_or_accepted(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
    malformed: contract.IndexState,
    valid: bool,
) -> None:
    observation = replace(malformed, valid=valid)
    monkeypatch.setattr(
        contract, "postgres_index_state", lambda _bind, **_kwargs: observation
    )
    statements: list[str] = []
    with pytest.raises(contract.RegionalReportIndexContractError) as failure:
        contract.ensure_postgres_index(bind, statements.append, spec=INVOICE)
    assert statements == []
    assert failure.value.spec == INVOICE
    assert failure.value.code == "migration.regional_report_index.contract_failed"


@pytest.mark.parametrize(
    "result", (None, replace(_state(), valid=False), replace(_state(), ready=False))
)
def test_creation_cannot_finish_without_verified_catalog_state(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
    result: contract.IndexState | None,
) -> None:
    observations = iter((None, result))
    monkeypatch.setattr(
        contract, "postgres_index_state", lambda _bind, **_kwargs: next(observations)
    )
    statements: list[str] = []
    with pytest.raises(contract.RegionalReportIndexContractError):
        contract.ensure_postgres_index(bind, statements.append, spec=INVOICE)
    assert statements == [INVOICE.create_sql]


def test_interrupted_repair_propagates_and_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
) -> None:
    observations = iter(
        (replace(_state(), valid=False), replace(_state(), valid=False), _state())
    )
    monkeypatch.setattr(
        contract, "postgres_index_state", lambda _bind, **_kwargs: next(observations)
    )
    statements: list[str] = []

    def interrupted(sql: str) -> None:
        statements.append(sql)
        if sql == INVOICE.create_sql:
            raise TimeoutError("simulated concurrent build interruption")

    with pytest.raises(TimeoutError):
        contract.ensure_postgres_index(bind, interrupted, spec=INVOICE)
    assert (
        contract.ensure_postgres_index(bind, statements.append, spec=INVOICE)
        is contract.RepairOutcome.rebuilt
    )
    assert statements == [INVOICE.drop_sql, INVOICE.create_sql] * 2


def test_payment_index_is_not_rebuilt_when_only_invoice_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
) -> None:
    observations = iter((replace(_state(), valid=False), _state(), _state(PAYMENT)))
    monkeypatch.setattr(
        contract, "postgres_index_state", lambda _bind, **_kwargs: next(observations)
    )
    statements: list[str] = []
    assert contract.ensure_postgres_indexes(bind, statements.append) == (
        contract.RepairOutcome.rebuilt,
        contract.RepairOutcome.unchanged,
    )
    assert statements == [INVOICE.drop_sql, INVOICE.create_sql]


@pytest.mark.parametrize("spec", contract.INDEX_SPECS)
def test_read_only_validation_requires_each_exact_index(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
    spec: contract.IndexSpec,
) -> None:
    def observation(
        _bind: sa.engine.Connection, *, spec: contract.IndexSpec
    ) -> contract.IndexState:
        return _state(spec)

    monkeypatch.setattr(contract, "postgres_index_state", observation)
    contract.validate_postgres_indexes(bind)
    monkeypatch.setattr(
        contract,
        "postgres_index_state",
        lambda _bind, **kwargs: (
            None if kwargs["spec"] == spec else _state(kwargs["spec"])
        ),
    )
    with pytest.raises(contract.RegionalReportIndexContractError, match="missing"):
        contract.validate_postgres_indexes(bind)


@pytest.mark.parametrize(
    "revision",
    (
        "658_regional_report_billing_indexes",
        "662_validate_regional_report_billing_indexes",
    ),
)
def test_migrations_delegate_to_same_contract_in_autocommit(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
    revision: str,
) -> None:
    path = Path("alembic/versions") / f"{revision}.py"
    module_spec = importlib.util.spec_from_file_location(revision, path)
    assert module_spec is not None and module_spec.loader is not None
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    calls: list[str] = []

    class Autocommit:
        def __enter__(self) -> None:
            calls.append("enter")

        def __exit__(self, *_args: object) -> None:
            calls.append("exit")

    postgres = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(migration.op, "get_bind", lambda: postgres)
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=Autocommit),
    )
    monkeypatch.setattr(
        migration, "ensure_postgres_indexes", lambda *_args: calls.append("ensure")
    )
    migration.upgrade()
    assert calls == ["enter", "ensure", "exit"]
    if revision.startswith("662"):
        assert migration.down_revision == "661_support_ticket_comment_idempotency"
        migration.downgrade()
        assert calls == ["enter", "ensure", "exit"]


def test_revision_658_keeps_sqlite_unit_lane_behavior(
    monkeypatch: pytest.MonkeyPatch,
    bind: sa.engine.Connection,
) -> None:
    path = Path("alembic/versions/658_regional_report_billing_indexes.py")
    module_spec = importlib.util.spec_from_file_location("regional_sqlite", path)
    assert module_spec is not None and module_spec.loader is not None
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=nullcontext),
    )
    migration.upgrade()
    assert statements == [
        f"CREATE INDEX IF NOT EXISTS {spec.name} ON {spec.table} ({', '.join(spec.keys)})"
        for spec in contract.INDEX_SPECS
    ]
