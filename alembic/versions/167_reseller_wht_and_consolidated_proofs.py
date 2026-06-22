"""Reseller withholding tax + consolidated (billing-account) payment proofs.

Adds the data behind reseller bulk payments that are made net of withholding
tax via bank transfer:

* ``payment_proofs.account_id`` becomes nullable and a ``billing_account_id`` is
  added so a proof can target a reseller's consolidated billing account instead
  of a single subscriber.
* ``payment_proofs`` gains ``gross_amount`` / ``wht_amount`` / ``wht_rate`` so a
  WHT-deducted transfer records the billed gross alongside the net cash paid.
* New ``withholding_tax_records`` table tracks the WHT receivable raised on
  verification (credit the gross, reclaim the withheld tax against the
  reseller's certificate later).

All operations are guarded so re-running against a DB that already has them is a
no-op.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "167_reseller_wht_and_consolidated_proofs"
down_revision = "166_customer_notification_read_at"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()

    # payment_proofs.account_id -> nullable (a proof may target a billing account)
    if _has_column("payment_proofs", "account_id") and bind.dialect.name != "sqlite":
        op.alter_column(
            "payment_proofs",
            "account_id",
            existing_type=UUID(as_uuid=True),
            nullable=True,
        )

    if not _has_column("payment_proofs", "billing_account_id"):
        op.add_column(
            "payment_proofs",
            sa.Column(
                "billing_account_id",
                UUID(as_uuid=True),
                sa.ForeignKey("billing_accounts.id"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_payment_proofs_billing_account_id",
            "payment_proofs",
            ["billing_account_id"],
        )
    for col in ("gross_amount", "wht_amount"):
        if not _has_column("payment_proofs", col):
            op.add_column(
                "payment_proofs", sa.Column(col, sa.Numeric(12, 2), nullable=True)
            )
    if not _has_column("payment_proofs", "wht_rate"):
        op.add_column(
            "payment_proofs", sa.Column("wht_rate", sa.Numeric(5, 2), nullable=True)
        )

    if not _has_table("withholding_tax_records"):
        op.create_table(
            "withholding_tax_records",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
            ),
            sa.Column(
                "billing_account_id",
                UUID(as_uuid=True),
                sa.ForeignKey("billing_accounts.id"),
                nullable=False,
            ),
            sa.Column(
                "reseller_id",
                UUID(as_uuid=True),
                sa.ForeignKey("resellers.id"),
                nullable=True,
            ),
            sa.Column(
                "payment_id",
                UUID(as_uuid=True),
                sa.ForeignKey("payments.id"),
                nullable=True,
            ),
            sa.Column(
                "payment_proof_id",
                UUID(as_uuid=True),
                sa.ForeignKey("payment_proofs.id"),
                nullable=True,
            ),
            sa.Column("gross_amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("net_amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("wht_amount", sa.Numeric(12, 2), nullable=False),
            sa.Column("wht_rate", sa.Numeric(5, 2), nullable=True),
            sa.Column("currency", sa.String(3), nullable=True),
            sa.Column(
                "status",
                sa.Enum(
                    "pending",
                    "certified",
                    "reclaimed",
                    "written_off",
                    name="withholdingtaxstatus",
                ),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("certificate_path", sa.String(500), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_withholding_tax_records_billing_account_id",
            "withholding_tax_records",
            ["billing_account_id"],
        )
        op.create_index(
            "ix_withholding_tax_records_reseller_id",
            "withholding_tax_records",
            ["reseller_id"],
        )
        op.create_index(
            "ix_withholding_tax_records_status",
            "withholding_tax_records",
            ["status"],
        )


def downgrade() -> None:
    if _has_table("withholding_tax_records"):
        op.drop_table("withholding_tax_records")
        sa.Enum(name="withholdingtaxstatus").drop(op.get_bind(), checkfirst=True)
    for col in ("wht_rate", "wht_amount", "gross_amount"):
        if _has_column("payment_proofs", col):
            op.drop_column("payment_proofs", col)
    if _has_column("payment_proofs", "billing_account_id"):
        op.drop_index(
            "ix_payment_proofs_billing_account_id", table_name="payment_proofs"
        )
        op.drop_column("payment_proofs", "billing_account_id")
    if (
        _has_column("payment_proofs", "account_id")
        and op.get_bind().dialect.name != "sqlite"
    ):
        op.alter_column(
            "payment_proofs",
            "account_id",
            existing_type=UUID(as_uuid=True),
            nullable=False,
        )
