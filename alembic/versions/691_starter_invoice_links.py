"""Structural Sub to Starter invoice link for thin-client issuance.

Revision ID: 691_starter_invoice_links
Revises: 690_backdated_invoice_permissions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "691_starter_invoice_links"
down_revision: str | None = "690_backdated_invoice_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "starter_invoice_links"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        _TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("billing_account_id", sa.UUID(), nullable=False),
        sa.Column("obligation_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("source_fact_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id", name="uq_starter_invoice_link_invoice"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_starter_invoice_link_scope"),
        sa.UniqueConstraint("document_id", name="uq_starter_invoice_link_document"),
    )
    op.create_index(
        "ix_starter_invoice_links_billing_account",
        _TABLE,
        ["billing_account_id"],
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
    op.drop_index("ix_starter_invoice_links_billing_account", table_name=_TABLE)
    op.drop_table(_TABLE)
