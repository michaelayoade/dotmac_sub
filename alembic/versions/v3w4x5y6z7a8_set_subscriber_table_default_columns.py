"""Set subscriber table default columns.

Revision ID: v3w4x5y6z7a8
Revises: u2v3w4x5y6z7
Create Date: 2026-02-25 14:25:00.000000
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v3w4x5y6z7a8"
down_revision: str | None = "u2v3w4x5y6z7"
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


def _seed_subscriber_defaults() -> None:
    subscriber_columns = [
        "subscriber_number",
        "subscriber_name",
        "status",
        "reseller_id",
        "id",
        "account_number",
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
        "created_at",
        "updated_at",
    ]
    visible_columns = {
        "subscriber_number",
        "subscriber_name",
        "status",
        "reseller_id",
    }

    now = datetime.now(UTC)
    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key = :table_key"
        ).bindparams(table_key="subscribers")
    )
    op.bulk_insert(
        _table_shape(),
        [
            {
                "id": uuid.uuid4(),
                "table_key": "subscribers",
                "column_key": key,
                "display_order": index,
                "is_visible": key in visible_columns,
                "created_at": now,
                "updated_at": now,
            }
            for index, key in enumerate(subscriber_columns)
        ],
    )


def upgrade() -> None:
    _seed_subscriber_defaults()


def downgrade() -> None:
    # Keep downgrade simple and deterministic: clear subscribers defaults.
    op.execute(
        sa.text(
            "DELETE FROM table_column_default_config WHERE table_key = :table_key"
        ).bindparams(table_key="subscribers")
    )

