"""Add the typed Deposit Account Credit intent contract.

Revision ID: 349_account_credit_deposit_lifecycle
Revises: 348_location_capture_prompt_state
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "349_account_credit_deposit_lifecycle"
down_revision = "348_location_capture_prompt_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "topup_intents",
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("topup_intents", sa.Column("purpose", sa.String(40)))
    op.add_column("topup_intents", sa.Column("allocation_policy", sa.String(40)))
    op.add_column(
        "topup_intents", sa.Column("credit_application_policy", sa.String(40))
    )
    op.add_column("topup_intents", sa.Column("policy_version", sa.Integer()))
    op.add_column("topup_intents", sa.Column("preview_fingerprint", sa.String(64)))
    op.add_column("topup_intents", sa.Column("idempotency_key", sa.String(120)))
    op.add_column("topup_intents", sa.Column("channel", sa.String(40)))
    op.add_column("topup_intents", sa.Column("created_by", sa.String(120)))
    op.create_foreign_key(
        "fk_topup_intents_provider_id_payment_providers",
        "topup_intents",
        "payment_providers",
        ["provider_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_topup_intents_deposit_idempotency",
        "topup_intents",
        ["account_id", "purpose", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'account_credit_deposit' AND idempotency_key IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "purpose = 'account_credit_deposit' AND idempotency_key IS NOT NULL"
        ),
    )
    op.create_check_constraint(
        "ck_topup_intents_account_credit_contract",
        "topup_intents",
        "purpose IS NULL OR ("
        "purpose = 'account_credit_deposit' AND "
        "allocation_policy = 'credit_only' AND "
        "credit_application_policy = 'pay_eligible_invoices' AND "
        "policy_version = 1 AND preview_fingerprint IS NOT NULL AND "
        "idempotency_key IS NOT NULL AND channel IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_topup_intents_account_credit_contract", "topup_intents", type_="check"
    )
    op.drop_index("uq_topup_intents_deposit_idempotency", table_name="topup_intents")
    op.drop_constraint(
        "fk_topup_intents_provider_id_payment_providers",
        "topup_intents",
        type_="foreignkey",
    )
    for column in (
        "created_by",
        "channel",
        "idempotency_key",
        "preview_fingerprint",
        "policy_version",
        "credit_application_policy",
        "allocation_policy",
        "purpose",
        "provider_id",
    ):
        op.drop_column("topup_intents", column)
