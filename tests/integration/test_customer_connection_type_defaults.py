"""Native inserts exercise connection type defaults on the migrated schema."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine


@pytest.mark.parametrize("explicit_values", [False, True])
def test_native_connection_type_insert_defaults_and_explicit_values(
    engine: Engine, explicit_values: bool
) -> None:
    identifier = uuid4()
    code = f"qa-defaults-{identifier.hex}"
    statement = (
        "INSERT INTO customer_connection_types "
        "(id, code, name, created_at, updated_at) "
        "VALUES (:id, :code, :name, now(), now()) "
        "RETURNING sort_order, is_active"
    )
    if explicit_values:
        statement = (
            "INSERT INTO customer_connection_types "
            "(id, code, name, created_at, updated_at, sort_order, is_active) "
            "VALUES (:id, :code, :name, now(), now(), 73, false) "
            "RETURNING sort_order, is_active"
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            row = connection.execute(
                text(statement),
                {"id": identifier, "code": code, "name": f"QA defaults {identifier}"},
            ).one()
            assert tuple(row) == ((73, False) if explicit_values else (0, True))
        finally:
            transaction.rollback()
