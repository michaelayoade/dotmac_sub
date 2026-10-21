"""Per-line Starter obligation links for multi-line thin-client issuance.

Revision ID: 692_starter_invoice_line_links
Revises: 691_starter_invoice_links
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "692_starter_invoice_line_links"
down_revision: str | None = "691_starter_invoice_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "starter_invoice_line_links"
_PARENT = "starter_invoice_links"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # A multi-line Starter document carries one obligation per line; the
    # invoice-level link then holds account/document identity only.
    op.alter_column(_PARENT, "obligation_id", nullable=True)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("invoice_line_id", sa.UUID(), nullable=False),
        sa.Column("obligation_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_line_id"], ["invoice_lines.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "invoice_line_id", name="uq_starter_invoice_line_link_line"
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_starter_invoice_line_link_scope"
        ),
        sa.UniqueConstraint(
            "obligation_id", name="uq_starter_invoice_line_link_obligation"
        ),
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
    op.alter_column(_PARENT, "obligation_id", nullable=False)
