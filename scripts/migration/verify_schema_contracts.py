"""Fail a deployment when required database structures are not usable.

Run after ``alembic upgrade heads`` and before replacing live services.  The
check is read-only and deliberately examines PostgreSQL catalog validity, not
only object names.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from app.db import SessionLocal
from scripts.migration.radius_session_latest_index import (
    invalid_postgres_indexes,
    validate_postgres_index,
)


def verify_schema_contracts(bind: Connection) -> None:
    if bind.dialect.name != "postgresql":
        return

    validate_postgres_index(bind)
    invalid = invalid_postgres_indexes(bind)
    if invalid:
        rendered = ", ".join(
            f"{schema}.{index} on {table}" for schema, table, index in invalid
        )
        raise RuntimeError(
            "database contains invalid or unready indexes after migration: " + rendered
        )


def main() -> None:
    db = SessionLocal()
    try:
        verify_schema_contracts(db.connection())
    finally:
        db.close()
    print("Database schema contracts verified.")


if __name__ == "__main__":
    main()
