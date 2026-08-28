"""Add durable top-up reconciliation attempt leases and provider rotation.

Revision ID: 563_topup_reconcile_leases
Revises: 562_topup_reconcile_progress
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "563_topup_reconcile_leases"
down_revision: str | None = "562_topup_reconcile_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUE_INDEX = "ix_topup_intents_gateway_reconcile_due"
_PROVIDER_ATTEMPT_INDEX = "ix_topup_intents_provider_reconcile_attempt"
_DUE_INDEX_COLUMNS = (
    "provider_type",
    "status",
    "gateway_next_reconcile_at",
    "gateway_last_reconcile_attempt_at",
    "created_at",
)
_DUE_INDEX_PREDICATE = (
    "completed_payment_id IS NULL AND status IN "
    "('pending', 'failed', 'abandoned', 'canceled', 'expired')"
)
_PROVIDER_ATTEMPT_INDEX_COLUMNS = (
    "provider_type",
    "gateway_last_reconcile_attempt_at",
)
_PROVIDER_ATTEMPT_INDEX_PREDICATE = "gateway_last_reconcile_attempt_at IS NOT NULL"
_PREDECESSOR_DUE_INDEX_COLUMNS = (
    "provider_type",
    "status",
    "completed_payment_id",
    "gateway_next_reconcile_at",
    "created_at",
)


def _column_names(bind) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns("topup_intents")}


def _add_attempt_columns() -> None:
    bind = op.get_bind()
    columns = _column_names(bind)
    if "gateway_last_reconcile_attempt_at" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column("gateway_last_reconcile_attempt_at", sa.DateTime(timezone=True)),
        )
    if "gateway_reconcile_attempt_count" not in columns:
        op.add_column(
            "topup_intents",
            sa.Column(
                "gateway_reconcile_attempt_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def _postgresql_index_state(bind, index_name: str) -> tuple[bool, str] | None:
    row = bind.execute(
        sa.text(
            "SELECT idx.indisvalid, pg_get_indexdef(idx.indexrelid) "
            "FROM pg_index AS idx "
            "WHERE idx.indexrelid = to_regclass(:index_name)"
        ),
        {"index_name": index_name},
    ).one_or_none()
    if row is None:
        return None
    return bool(row[0]), str(row[1])


def _definition_matches(
    definition: str,
    *,
    columns: tuple[str, ...],
    predicate_fragments: tuple[str, ...] = (),
) -> bool:
    normalized = " ".join(definition.lower().split())
    expected_columns = f"({', '.join(columns)})"
    if expected_columns not in normalized:
        return False
    if predicate_fragments:
        return " where " in normalized and all(
            fragment in normalized for fragment in predicate_fragments
        )
    return " where " not in normalized


def _ensure_postgresql_index(
    bind,
    *,
    index_name: str,
    create_sql: str,
    columns: tuple[str, ...],
    predicate_fragments: tuple[str, ...] = (),
) -> None:
    state = _postgresql_index_state(bind, index_name)
    if (
        state is not None
        and state[0]
        and _definition_matches(
            state[1],
            columns=columns,
            predicate_fragments=predicate_fragments,
        )
    ):
        return
    if state is not None:
        # A canceled CREATE INDEX CONCURRENTLY leaves an invalid relation, while
        # revision 562 leaves the valid predecessor definition under this name.
        # Both must be removed; IF NOT EXISTS would silently accept either one.
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
    with op.get_context().autocommit_block():
        op.execute(create_sql)
    state = _postgresql_index_state(bind, index_name)
    if (
        state is None
        or not state[0]
        or not _definition_matches(
            state[1],
            columns=columns,
            predicate_fragments=predicate_fragments,
        )
    ):
        raise RuntimeError(
            f"revision 563 requires the expected valid {index_name} index"
        )


def _drop_postgresql_index(index_name: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def _upgrade_postgresql_indexes(bind) -> None:
    _ensure_postgresql_index(
        bind,
        index_name=_DUE_INDEX,
        create_sql=(
            f"CREATE INDEX CONCURRENTLY {_DUE_INDEX} ON topup_intents "
            f"({', '.join(_DUE_INDEX_COLUMNS)}) WHERE {_DUE_INDEX_PREDICATE}"
        ),
        columns=_DUE_INDEX_COLUMNS,
        predicate_fragments=(
            "completed_payment_id is null",
            "pending",
            "failed",
            "abandoned",
            "canceled",
            "expired",
        ),
    )
    _ensure_postgresql_index(
        bind,
        index_name=_PROVIDER_ATTEMPT_INDEX,
        create_sql=(
            f"CREATE INDEX CONCURRENTLY {_PROVIDER_ATTEMPT_INDEX} "
            f"ON topup_intents ({', '.join(_PROVIDER_ATTEMPT_INDEX_COLUMNS)}) "
            f"WHERE {_PROVIDER_ATTEMPT_INDEX_PREDICATE}"
        ),
        columns=_PROVIDER_ATTEMPT_INDEX_COLUMNS,
        predicate_fragments=("gateway_last_reconcile_attempt_at is not null",),
    )


def _upgrade_sqlite_indexes(bind) -> None:
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("topup_intents")}
    if _DUE_INDEX in indexes:
        op.drop_index(_DUE_INDEX, table_name="topup_intents")
    op.create_index(
        _DUE_INDEX,
        "topup_intents",
        list(_DUE_INDEX_COLUMNS),
        sqlite_where=sa.text(_DUE_INDEX_PREDICATE),
    )
    if _PROVIDER_ATTEMPT_INDEX not in indexes:
        op.create_index(
            _PROVIDER_ATTEMPT_INDEX,
            "topup_intents",
            list(_PROVIDER_ATTEMPT_INDEX_COLUMNS),
            sqlite_where=sa.text(_PROVIDER_ATTEMPT_INDEX_PREDICATE),
        )


def upgrade() -> None:
    bind = op.get_bind()
    if "topup_intents" not in set(sa.inspect(bind).get_table_names()):
        raise RuntimeError("revision 563 requires the topup_intents table")
    _add_attempt_columns()
    required = {
        "gateway_last_reconcile_attempt_at",
        "gateway_reconcile_attempt_count",
    }
    missing = required - _column_names(bind)
    if missing:
        raise RuntimeError(
            "revision 563 could not add required top-up reconciliation columns: "
            + ", ".join(sorted(missing))
        )
    if bind.dialect.name == "postgresql":
        _upgrade_postgresql_indexes(bind)
    else:
        _upgrade_sqlite_indexes(bind)


def _downgrade_postgresql_indexes(bind) -> None:
    _drop_postgresql_index(_PROVIDER_ATTEMPT_INDEX)
    _ensure_postgresql_index(
        bind,
        index_name=_DUE_INDEX,
        create_sql=(
            f"CREATE INDEX CONCURRENTLY {_DUE_INDEX} ON topup_intents "
            f"({', '.join(_PREDECESSOR_DUE_INDEX_COLUMNS)})"
        ),
        columns=_PREDECESSOR_DUE_INDEX_COLUMNS,
    )


def _downgrade_sqlite_indexes(bind) -> None:
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("topup_intents")}
    if _PROVIDER_ATTEMPT_INDEX in indexes:
        op.drop_index(_PROVIDER_ATTEMPT_INDEX, table_name="topup_intents")
    if _DUE_INDEX in indexes:
        op.drop_index(_DUE_INDEX, table_name="topup_intents")
    op.create_index(
        _DUE_INDEX,
        "topup_intents",
        list(_PREDECESSOR_DUE_INDEX_COLUMNS),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "topup_intents" not in set(sa.inspect(bind).get_table_names()):
        raise RuntimeError("revision 563 requires the topup_intents table")
    if bind.dialect.name == "postgresql":
        _downgrade_postgresql_indexes(bind)
    else:
        _downgrade_sqlite_indexes(bind)
    columns = _column_names(bind)
    for column in (
        "gateway_reconcile_attempt_count",
        "gateway_last_reconcile_attempt_at",
    ):
        if column in columns:
            op.drop_column("topup_intents", column)
