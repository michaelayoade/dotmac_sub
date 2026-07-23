from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.migration import radius_session_latest_index as contract


def _state(**overrides) -> contract.IndexState:
    values = {
        "table_name": contract.TABLE_NAME,
        "valid": True,
        "ready": True,
        "unique": False,
        "access_method": "btree",
        "key_attribute_count": 3,
        "total_attribute_count": 3,
        "has_predicate": False,
        "keys": contract.EXPECTED_KEYS,
    }
    values.update(overrides)
    return contract.IndexState(**values)


def _bind():
    return SimpleNamespace()


def test_missing_index_is_built_and_verified(monkeypatch) -> None:
    states = iter([None, _state()])
    monkeypatch.setattr(contract, "postgres_index_state", lambda _bind: next(states))
    executed: list[str] = []

    contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == [contract.CREATE_POSTGRES_SQL]


def test_interrupted_concurrent_build_is_dropped_then_rebuilt(monkeypatch) -> None:
    states = iter([_state(valid=False), _state()])
    monkeypatch.setattr(contract, "postgres_index_state", lambda _bind: next(states))
    executed: list[str] = []

    contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == [contract.DROP_POSTGRES_SQL, contract.CREATE_POSTGRES_SQL]


def test_rebuild_must_be_catalog_valid_before_migration_can_finish(
    monkeypatch,
) -> None:
    states = iter([None, _state(valid=False)])
    monkeypatch.setattr(contract, "postgres_index_state", lambda _bind: next(states))
    executed: list[str] = []

    with pytest.raises(RuntimeError, match="is not valid"):
        contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == [contract.CREATE_POSTGRES_SQL]


def test_valid_malformed_index_is_rejected_not_replaced(monkeypatch) -> None:
    malformed = _state(keys=("subscription_id", "id DESC", "created_at DESC"))
    states = iter([malformed, malformed])
    monkeypatch.setattr(contract, "postgres_index_state", lambda _bind: next(states))
    executed: list[str] = []

    with pytest.raises(RuntimeError, match="key definition"):
        contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == []


def test_valid_partial_or_covering_index_is_not_accepted() -> None:
    partial_errors = contract.index_contract_errors(_state(has_predicate=True))
    covering_errors = contract.index_contract_errors(_state(total_attribute_count=4))

    assert any("partial index" in error for error in partial_errors)
    assert any("included attributes" in error for error in covering_errors)


def test_same_name_on_another_table_is_never_dropped(monkeypatch) -> None:
    monkeypatch.setattr(
        contract,
        "postgres_index_state",
        lambda _bind: _state(table_name="another_table", valid=False),
    )
    executed: list[str] = []

    with pytest.raises(RuntimeError, match="refusing to replace"):
        contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == []


def test_exact_valid_index_is_an_idempotent_noop(monkeypatch) -> None:
    valid = _state()
    states = iter([valid, valid])
    monkeypatch.setattr(contract, "postgres_index_state", lambda _bind: next(states))
    executed: list[str] = []

    contract.ensure_postgres_index(_bind(), executed.append)

    assert executed == []
