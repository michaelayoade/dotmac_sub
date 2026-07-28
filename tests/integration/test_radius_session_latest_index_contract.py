from __future__ import annotations

from scripts.migration.radius_session_latest_index import (
    index_contract_errors,
    postgres_index_state,
)


def test_postgres_catalog_matches_latest_session_index_contract(db_session) -> None:
    state = postgres_index_state(db_session.connection())

    assert index_contract_errors(state) == []
