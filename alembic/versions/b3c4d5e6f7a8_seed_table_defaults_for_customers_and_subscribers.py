"""seed system table defaults for customers and subscribers

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-02-24 16:10:00.000000
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_shape() -> sa.Table:
    return sa.table(
        "table_column_default_config",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("table_key", sa.String),
        sa.column("column_key", sa.String),
        sa.column("display_order", sa.Integer),
        sa.column("is_visible", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _seed_table_defaults(
    table_key: str,
    ordered_columns: list[str],
    visible_columns: set[str],
) -> None:
    now = datetime.now(UTC)

    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key = :table_key"
        ).bindparams(table_key=table_key)
    )

    rows = [
        {
            "id": uuid.uuid4(),
            "table_key": table_key,
            "column_key": key,
            "display_order": index,
            "is_visible": key in visible_columns,
            "created_at": now,
            "updated_at": now,
        }
        for index, key in enumerate(ordered_columns)
    ]

    op.bulk_insert(_table_shape(), rows)


def upgrade() -> None:
    customer_columns = [
        "customer_name",
        "id",
        "status",
        "activation_state",
        "customer_type",
        "email",
        "subscriber_number",
        "account_number",
        "first_name",
        "last_name",
        "is_active",
        "user_type",
        "billing_enabled",
        "marketing_opt_in",
        "created_at",
        "updated_at",
        "organization_id",
        "reseller_id",
        "min_balance",
        "approval_status",
        "tier_state",
    ]
    _seed_table_defaults(
        "customers",
        customer_columns,
        {
            "customer_name",
            "id",
            "status",
            "customer_type",
        },
    )

    subscriber_columns = [
        "id",
        "subscriber_number",
        "account_number",
        "status",
        "subscriber_name",
        "activation_state",
        "subscriber_type",
        "approval_status",
        "tier_state",
        "email",
        "phone",
        "first_name",
        "last_name",
        "is_active",
        "user_type",
        "billing_enabled",
        "marketing_opt_in",
        "organization_id",
        "reseller_id",
        "created_at",
        "updated_at",
    ]
    _seed_table_defaults(
        "subscribers",
        subscriber_columns,
        {
            "id",
            "subscriber_number",
            "account_number",
            "status",
        },
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key IN (:k1, :k2)"
        ).bindparams(k1="customers", k2="subscribers")
    )
