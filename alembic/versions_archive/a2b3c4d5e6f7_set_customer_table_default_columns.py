"""set customers system default table columns

Revision ID: a2b3c4d5e6f7
Revises: f1c2d3e4a5b6
Create Date: 2026-02-24 15:00:00.000000
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f1c2d3e4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = sa.table(
        "table_column_default_config",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("table_key", sa.String),
        sa.column("column_key", sa.String),
        sa.column("display_order", sa.Integer),
        sa.column("is_visible", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    all_customer_columns = [
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
    visible_columns = {
        "customer_name",
        "id",
        "status",
        "activation_state",
        "customer_type",
    }

    now = datetime.now(UTC)

    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key = :table_key"
        ).bindparams(table_key="customers")
    )

    rows = [
        {
            "id": uuid.uuid4(),
            "table_key": "customers",
            "column_key": key,
            "display_order": index,
            "is_visible": key in visible_columns,
            "created_at": now,
            "updated_at": now,
        }
        for index, key in enumerate(all_customer_columns)
    ]

    op.bulk_insert(table, rows)


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key = :table_key"
        ).bindparams(table_key="customers")
    )
