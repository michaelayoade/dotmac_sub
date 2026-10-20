"""Structural Sub to Starter prepaid funding link.

Revision ID: 696_starter_prepaid_funding_links
Revises: 695_subscription_credential_reveal
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "696_starter_prepaid_funding_links"
down_revision: str | None = "695_subscription_credential_reveal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "starter_prepaid_funding_links"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Prepaid periods funded through Starter record which settlement
    # earmarked their funding structurally, never in metadata (ADR 0007).
    op.create_table(
        _TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("subscription_id", sa.UUID(), nullable=False),
        sa.Column("billing_line_key", sa.String(255), nullable=False),
        sa.Column("source_fact_id", sa.String(255), nullable=False),
        sa.Column("billing_account_id", sa.UUID(), nullable=False),
        sa.Column("settlement_id", sa.UUID(), nullable=False),
        sa.Column("confirmation_id", sa.UUID(), nullable=False),
        sa.Column("purchase_ref", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "billing_line_key",
            name="uq_starter_prepaid_funding_link_period",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_starter_prepaid_funding_link_scope"
        ),
    )
    op.create_index(
        "ix_starter_prepaid_funding_links_billing_account",
        _TABLE,
        ["billing_account_id"],
    )
    op.create_index(
        "ix_starter_prepaid_funding_links_settlement", _TABLE, ["settlement_id"]
    )
    op.execute(f"ALTER TABLE public.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_tenant_isolation ON public.{_TABLE} "
        "USING (tenant_id = app_current_tenant_id()) "
        "WITH CHECK (tenant_id = app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TABLE} TO app_user")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.drop_table(_TABLE)
