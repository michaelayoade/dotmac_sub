"""Pure tests for migration schema operation helpers."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.migration_schema_ops import declared_idempotent_schema_create_target


@pytest.mark.parametrize(
    ("statement", "expected"),
    (
        ("CREATE SCHEMA IF NOT EXISTS mod_payments;", "mod_payments"),
        ("CREATE SCHEMA mod_coll;", "mod_coll"),
        ('CREATE SCHEMA "mod_coll";', "mod_coll"),
        ("CREATE SCHEMA mod_coll AUTHORIZATION dotmac_app;", "mod_coll"),
        (text("CREATE SCHEMA mod_coll;"), "mod_coll"),
        ("CREATE SCHEMA public;", None),
        ("DROP SCHEMA mod_coll;", None),
    ),
)
def test_declared_schema_create_target_accepts_module_schema_forms(
    statement: object, expected: str | None
) -> None:
    assert declared_idempotent_schema_create_target(statement) == expected
