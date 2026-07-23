"""Structural contract for the latest RADIUS-session projection index.

The accounting table is hot and large, so PostgreSQL must build this index
concurrently.  A failed concurrent build leaves an index object behind with
``indisvalid = false``; checking only the index name would then turn a retry
into a false success.  Migrations and deploy verification share this contract
so existence, validity, readiness, ownership, and key expressions are checked
together.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import sqlalchemy as sa

SCHEMA_NAME = "public"
TABLE_NAME = "radius_accounting_sessions"
INDEX_NAME = "ix_radius_accounting_sessions_subscription_latest"
INDEX_EXPRESSION = (
    "subscription_id, "
    "(COALESCE(last_update_at, session_start, created_at)) DESC, "
    "id DESC"
)
EXPECTED_KEYS = (
    "subscription_id",
    "COALESCE(last_update_at, session_start, created_at) DESC",
    "id DESC",
)

CREATE_POSTGRES_SQL = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    f"ON {SCHEMA_NAME}.{TABLE_NAME} ({INDEX_EXPRESSION})"
)
DROP_POSTGRES_SQL = f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA_NAME}.{INDEX_NAME}"


@dataclass(frozen=True)
class IndexState:
    table_name: str
    valid: bool
    ready: bool
    unique: bool
    access_method: str
    key_attribute_count: int
    total_attribute_count: int
    has_predicate: bool
    keys: tuple[str, ...]


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace('"', "").strip()).casefold()


def postgres_index_state(bind: sa.engine.Connection) -> IndexState | None:
    """Return the catalog state for the named index, including invalid builds."""

    row = (
        bind.execute(
            sa.text(
                """
                SELECT
                    table_class.relname AS table_name,
                    index_data.indisvalid AS valid,
                    index_data.indisready AS ready,
                    index_data.indisunique AS unique,
                    index_data.indnkeyatts AS key_attribute_count,
                    index_data.indnatts AS total_attribute_count,
                    index_data.indpred IS NOT NULL AS has_predicate,
                    access_method.amname AS access_method,
                    pg_get_indexdef(index_data.indexrelid, 1, true) AS key_1,
                    pg_get_indexdef(index_data.indexrelid, 2, true) AS key_2,
                    pg_get_indexdef(index_data.indexrelid, 3, true) AS key_3
                FROM pg_class AS index_class
                JOIN pg_namespace AS index_namespace
                  ON index_namespace.oid = index_class.relnamespace
                JOIN pg_index AS index_data
                  ON index_data.indexrelid = index_class.oid
                JOIN pg_class AS table_class
                  ON table_class.oid = index_data.indrelid
                JOIN pg_am AS access_method
                  ON access_method.oid = index_class.relam
                WHERE index_namespace.nspname = :schema_name
                  AND index_class.relname = :index_name
                """
            ),
            {"schema_name": SCHEMA_NAME, "index_name": INDEX_NAME},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return IndexState(
        table_name=str(row["table_name"]),
        valid=bool(row["valid"]),
        ready=bool(row["ready"]),
        unique=bool(row["unique"]),
        access_method=str(row["access_method"]),
        key_attribute_count=int(row["key_attribute_count"]),
        total_attribute_count=int(row["total_attribute_count"]),
        has_predicate=bool(row["has_predicate"]),
        keys=tuple(str(row[f"key_{position}"] or "") for position in range(1, 4)),
    )


def index_contract_errors(state: IndexState | None) -> list[str]:
    if state is None:
        return [f"{SCHEMA_NAME}.{INDEX_NAME} is missing"]

    errors: list[str] = []
    if state.table_name != TABLE_NAME:
        errors.append(
            f"{SCHEMA_NAME}.{INDEX_NAME} belongs to {state.table_name}, "
            f"expected {TABLE_NAME}"
        )
    if not state.valid:
        errors.append(f"{SCHEMA_NAME}.{INDEX_NAME} is not valid")
    if not state.ready:
        errors.append(f"{SCHEMA_NAME}.{INDEX_NAME} is not ready")
    if state.unique:
        errors.append(f"{SCHEMA_NAME}.{INDEX_NAME} must not be unique")
    if state.access_method != "btree":
        errors.append(
            f"{SCHEMA_NAME}.{INDEX_NAME} uses {state.access_method}, expected btree"
        )
    if state.key_attribute_count != len(EXPECTED_KEYS):
        errors.append(
            f"{SCHEMA_NAME}.{INDEX_NAME} has {state.key_attribute_count} key "
            f"attributes, expected {len(EXPECTED_KEYS)}"
        )
    if state.total_attribute_count != len(EXPECTED_KEYS):
        errors.append(
            f"{SCHEMA_NAME}.{INDEX_NAME} has included attributes; none are expected"
        )
    if state.has_predicate:
        errors.append(f"{SCHEMA_NAME}.{INDEX_NAME} must not be a partial index")
    if tuple(_normalized(key) for key in state.keys) != tuple(
        _normalized(key) for key in EXPECTED_KEYS
    ):
        errors.append(
            f"{SCHEMA_NAME}.{INDEX_NAME} key definition does not match "
            "the latest-session projection"
        )
    return errors


def validate_postgres_index(bind: sa.engine.Connection) -> None:
    errors = index_contract_errors(postgres_index_state(bind))
    if errors:
        raise RuntimeError(
            "RADIUS latest-session index contract failed: " + "; ".join(errors)
        )


def ensure_postgres_index(
    bind: sa.engine.Connection,
    execute: Callable[[str], None],
) -> None:
    """Create, validate, or safely replace an interrupted concurrent build."""

    state = postgres_index_state(bind)
    if state is not None and state.table_name != TABLE_NAME:
        raise RuntimeError(
            f"{SCHEMA_NAME}.{INDEX_NAME} belongs to {state.table_name}; "
            f"refusing to replace an index not owned by {TABLE_NAME}"
        )

    if state is not None and state.valid and state.ready:
        validate_postgres_index(bind)
        return

    if state is not None:
        execute(DROP_POSTGRES_SQL)
    execute(CREATE_POSTGRES_SQL)
    validate_postgres_index(bind)


def invalid_postgres_indexes(
    bind: sa.engine.Connection,
) -> tuple[tuple[str, str, str], ...]:
    """Return user-schema indexes that cannot safely serve queries."""

    rows = bind.execute(
        sa.text(
            """
            SELECT
                namespace.nspname AS schema_name,
                table_class.relname AS table_name,
                index_class.relname AS index_name
            FROM pg_index AS index_data
            JOIN pg_class AS index_class
              ON index_class.oid = index_data.indexrelid
            JOIN pg_class AS table_class
              ON table_class.oid = index_data.indrelid
            JOIN pg_namespace AS namespace
              ON namespace.oid = index_class.relnamespace
            WHERE (NOT index_data.indisvalid OR NOT index_data.indisready)
              AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
              AND namespace.nspname <> 'information_schema'
            ORDER BY namespace.nspname, table_class.relname, index_class.relname
            """
        )
    ).mappings()
    return tuple(
        (
            str(row["schema_name"]),
            str(row["table_name"]),
            str(row["index_name"]),
        )
        for row in rows
    )
