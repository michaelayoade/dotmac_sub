"""add_indexes_on_high_traffic_fk_columns

Revision ID: f88cb663b8e0
Revises: f4g7b9c1d3e5
Create Date: 2026-03-15 11:47:12.974220

"""

from sqlalchemy import inspect

from alembic import op

revision = "f88cb663b8e0"
down_revision = "f4g7b9c1d3e5"
branch_labels = None
depends_on = None

# (index_name, table_name, column_name)
INDEXES = [
    ("ix_subscriptions_subscriber_id", "subscriptions", "subscriber_id"),
    ("ix_invoices_account_id", "invoices", "account_id"),
    ("ix_service_orders_subscriber_id", "service_orders", "subscriber_id"),
    ("ix_usage_records_subscription_id", "usage_records", "subscription_id"),
    ("ix_ip_assignments_subscription_id", "ip_assignments", "subscription_id"),
    ("ix_payments_account_id", "payments", "account_id"),
    ("ix_ledger_entries_account_id", "ledger_entries", "account_id"),
    ("ix_dunning_cases_account_id", "dunning_cases", "account_id"),
]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    for index_name, table_name, column_name in INDEXES:
        if not inspector.has_table(table_name):
            continue
        col_names = {c["name"] for c in inspector.get_columns(table_name)}
        if column_name not in col_names:
            continue
        existing = [idx["name"] for idx in inspector.get_indexes(table_name)]
        if index_name not in existing:
            op.create_index(index_name, table_name, [column_name])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    for index_name, table_name, _column_name in reversed(INDEXES):
        if not inspector.has_table(table_name):
            continue
        existing = [idx["name"] for idx in inspector.get_indexes(table_name)]
        if index_name in existing:
            op.drop_index(index_name, table_name=table_name)
