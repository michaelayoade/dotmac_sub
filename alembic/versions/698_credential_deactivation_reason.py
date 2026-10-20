"""Record why an access credential was deactivated.

Additive and nullable: NULL means unknown or historical, with no backfill.
Values: ``service_ended`` (deactivated together with its service) and
``manual`` (an explicit administrative deactivation).

Revision ID: 698_credential_deactivation_reason
Revises: 697_subscription_credential_permission
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "698_credential_deactivation_reason"
down_revision: str | None = "697_subscription_credential_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "access_credentials" not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns("access_credentials")}
    if "deactivation_reason" in columns:
        return
    op.add_column(
        "access_credentials",
        sa.Column("deactivation_reason", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "access_credentials" not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns("access_credentials")}
    if "deactivation_reason" not in columns:
        return
    op.drop_column("access_credentials", "deactivation_reason")
