"""Published router-health defaults survive the metadata-backed squash.

The shared engine must point at the real Alembic-migrated PostgreSQL schema.
Native SQL deliberately bypasses SQLAlchemy's Python defaults. All inserts
roll back, and the catalog assertions also cover the FK-bound signal table
without manufacturing router inventory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine


def test_router_health_catalog_defaults_match_published_revision(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name, data_type, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND ("
                "(table_name = 'router_health_signals' AND column_name IN "
                "('samples', 'consecutive_abnormal')) OR "
                "(table_name = 'nas_coa_outcome_buckets' AND column_name = 'count'))"
            )
        )
        defaults: dict[tuple[str, str], tuple[str, str]] = {
            (row.table_name, row.column_name): (row.data_type, row.column_default)
            for row in rows
        }

    assert defaults == {
        ("router_health_signals", "samples"): ("json", "'[]'::json"),
        ("router_health_signals", "consecutive_abnormal"): ("integer", "0"),
        ("nas_coa_outcome_buckets", "count"): ("integer", "0"),
    }


@pytest.mark.parametrize("explicit_count", (None, 73))
def test_native_bucket_insert_default_preserves_explicit_count(
    engine: Engine, explicit_count: int | None
) -> None:
    bucket_id: UUID = uuid4()
    nas_device_id: UUID = uuid4()
    observed_at = datetime.now(UTC)
    if explicit_count is None:
        statement = text(
            "INSERT INTO public.nas_coa_outcome_buckets "
            "(id, nas_device_id, kind, disposition, bucket_start, last_at) "
            "VALUES (:id, :nas_device_id, 'canary', 'sent', :at, :at) "
            "RETURNING count"
        )
    else:
        statement = text(
            "INSERT INTO public.nas_coa_outcome_buckets "
            "(id, nas_device_id, kind, disposition, bucket_start, last_at, count) "
            "VALUES (:id, :nas_device_id, 'canary', 'sent', :at, :at, :count) "
            "RETURNING count"
        )

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            actual_count = connection.execute(
                statement,
                {
                    "id": bucket_id,
                    "nas_device_id": nas_device_id,
                    "at": observed_at,
                    "count": explicit_count,
                },
            ).scalar_one()
            assert actual_count == (0 if explicit_count is None else explicit_count)
        finally:
            transaction.rollback()
