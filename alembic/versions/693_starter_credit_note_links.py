"""Structural Sub to Starter credit-note link and invoice fact version.

Revision ID: 693_starter_credit_note_links
Revises: 692_starter_invoice_line_links
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "693_starter_credit_note_links"
down_revision: str | None = "692_starter_invoice_line_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "starter_credit_note_links"
_PARENT = "starter_invoice_links"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # Readers read the exact frozen document fact by (document, version);
    # issuance records the version it produced alongside the identity.
    op.add_column(
        _PARENT,
        sa.Column("issued_fact_version", sa.Integer(), nullable=True),
    )
    # Credit notes issued through Starter record their Starter document
    # identity structurally, never in metadata (ADR 0007).
    op.create_table(
        _TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("credit_note_id", sa.UUID(), nullable=False),
        sa.Column("billing_account_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("original_document_id", sa.UUID(), nullable=False),
        sa.Column("source_fact_id", sa.String(255), nullable=False),
        sa.Column("issued_fact_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["credit_note_id"], ["credit_notes.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credit_note_id", name="uq_starter_credit_note_link_note"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_starter_credit_note_link_scope"
        ),
        sa.UniqueConstraint("document_id", name="uq_starter_credit_note_link_document"),
    )
    op.create_index(
        "ix_starter_credit_note_links_billing_account",
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
    op.drop_table(_TABLE)
    op.drop_column(_PARENT, "issued_fact_version")
