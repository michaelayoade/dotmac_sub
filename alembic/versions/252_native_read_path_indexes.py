"""Performance: index the native (CRM-replacing) portal read paths.

Phase-3 backfilled projects / quotes / leads / sales-order and the maps
vendor-route tables (migration 244 + 248) but — because the read surfaces
were behind default-off flags (``*_native_read_enabled``) — never indexed the
columns those reads filter and join on. Postgres does not auto-index foreign
keys, so every ``/me/projects`` / ``/me/quotes`` (and reseller subtree) render
sequentially scans the table and defeats the ``selectinload`` child fetches.

Adds (see PR): projects/quotes subscriber scans (partial ``WHERE is_active``),
the ``selectinload`` child FKs (project_tasks / quote_line_items), the H1
functional lookup on ``projects.metadata->>'quote_id'`` (partial), the leads
subscriber scan, and the maps vendor-route + sales-order join FKs.

The functional index expression is written as
``CAST((metadata ->> 'quote_id') AS VARCHAR)`` to textually match what
SQLAlchemy's generic-JSON ``metadata_['quote_id'].as_string()`` compiles to
(the H1 resolver + the single-quote helper both use it), so the planner
actually uses it.

On Postgres the indexes build CONCURRENTLY (outside a transaction) so the
build never locks writes on these growing tables. All statements are
IF NOT EXISTS so this is idempotent and a no-op where the model-level Index()
already created them (fresh create_all).

Revision ID: 251_native_read_path_indexes
Revises: 250_field_material_request_erp_fields
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op

revision = "252_native_read_path_indexes"
down_revision = "251_event_handler_attempts"
branch_labels = None
depends_on = None

# Functional (H1) expression — must match .as_string() so the planner uses it.
_QUOTE_ID_EXPR = "CAST((metadata ->> 'quote_id') AS VARCHAR)"

# (index_name, table, "expr / col, col ...", partial_where_or_None)
_INDEXES: list[tuple[str, str, str, str | None]] = [
    # --- CRITICAL: primary filters of the native flip paths ---
    ("ix_projects_subscriber_id", "projects", "subscriber_id", "is_active"),
    ("ix_project_tasks_project_id", "project_tasks", "project_id", None),
    ("ix_quotes_subscriber_id", "quotes", "subscriber_id", "is_active"),
    ("ix_quote_line_items_quote_id", "quote_line_items", "quote_id", None),
    # --- HIGH ---
    ("ix_projects_metadata_quote_id", "projects", _QUOTE_ID_EXPR, "is_active"),
    ("ix_leads_subscriber_id", "leads", "subscriber_id", None),
    # --- MEDIUM: maps/vendor-route + sales join FKs ---
    ("ix_project_quotes_project_id", "project_quotes", "project_id", None),
    ("ix_project_quotes_vendor_id", "project_quotes", "vendor_id", None),
    ("ix_as_built_routes_project_id", "as_built_routes", "project_id", None),
    (
        "ix_installation_projects_subscriber_id",
        "installation_projects",
        "subscriber_id",
        None,
    ),
    (
        "ix_installation_projects_assigned_vendor_id",
        "installation_projects",
        "assigned_vendor_id",
        None,
    ),
    (
        "ix_installation_project_notes_project_id",
        "installation_project_notes",
        "project_id",
        None,
    ),
    ("ix_sales_orders_subscriber_id", "sales_orders", "subscriber_id", None),
    (
        "ix_sales_order_lines_sales_order_id",
        "sales_order_lines",
        "sales_order_id",
        None,
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY cannot run inside a transaction block.
        with op.get_context().autocommit_block():
            for name, table, expr, where in _INDEXES:
                where_sql = f" WHERE {where}" if where else ""
                op.execute(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                    f"ON {table} ({expr}){where_sql}"
                )
    else:
        for name, table, expr, where in _INDEXES:
            where_sql = f" WHERE {where}" if where else ""
            op.execute(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({expr}){where_sql}"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for name, _table, _expr, _where in _INDEXES:
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    else:
        for name, _table, _expr, _where in _INDEXES:
            op.execute(f"DROP INDEX IF EXISTS {name}")
