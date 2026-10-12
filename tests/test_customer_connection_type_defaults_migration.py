"""Fast PostgreSQL DDL contracts; deployed behavior is tested in integration."""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from types import ModuleType

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

from app.models.customer_connection_type import CustomerConnectionTypeDefinition


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/684_customer_connection_type_defaults.py"
    )
    spec = importlib.util.spec_from_file_location("migration_684_defaults", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_only_sets_canonical_defaults_with_bounded_locks() -> None:
    migration = _load_migration()
    assert migration.down_revision == "683_field_lifecycle_connection_types_merge"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        migration.upgrade()

    assert output.getvalue().splitlines() == [
        "SET LOCAL lock_timeout = '5s';",
        "",
        "SET LOCAL statement_timeout = '120s';",
        "",
        "ALTER TABLE customer_connection_types ALTER COLUMN sort_order SET DEFAULT '0';",
        "",
        "ALTER TABLE customer_connection_types ALTER COLUMN is_active SET DEFAULT true;",
        "",
    ]


def test_downgrade_preserves_defaults_owned_by_published_671() -> None:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        _load_migration().downgrade()

    assert output.getvalue() == ""


def test_bootstrap_model_declares_the_same_server_defaults() -> None:
    columns = CustomerConnectionTypeDefinition.__table__.c
    sort_default = columns.sort_order.server_default
    active_default = columns.is_active.server_default
    assert sort_default is not None and sort_default.arg == "0"
    assert active_default is not None
    assert str(active_default.arg.compile(dialect=postgresql.dialect())) == "true"
    assert columns.sort_order.nullable is False
    assert columns.is_active.nullable is False
