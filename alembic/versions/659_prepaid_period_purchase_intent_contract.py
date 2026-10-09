"""Extend top-up intent contract for prepaid period purchases.

Revision ID: 637_prepaid_period_purchase_intent_contract
Revises: 636_service_period_purchase_contract
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "637_prepaid_period_purchase_intent_contract"
down_revision: str | None = "636_service_period_purchase_contract"
branch_labels = None
depends_on = None


_CONTRACT = (
    "purpose IS NULL OR ("
    "purpose = 'account_credit_deposit' AND "
    "allocation_policy = 'credit_only' AND "
    "credit_application_policy = 'pay_eligible_invoices' AND "
    "policy_version = 1 AND preview_fingerprint IS NOT NULL AND "
    "idempotency_key IS NOT NULL AND channel IS NOT NULL) OR ("
    "purpose = 'prepaid_period_purchase' AND "
    "allocation_policy = 'selected_purchase_invoices_only' AND "
    "credit_application_policy = 'none' AND policy_version = 1 AND "
    "preview_fingerprint IS NOT NULL AND idempotency_key IS NOT NULL "
    "AND channel IS NOT NULL)"
)

_OLD_CONTRACT = (
    "purpose IS NULL OR ("
    "purpose = 'account_credit_deposit' AND "
    "allocation_policy = 'credit_only' AND "
    "credit_application_policy = 'pay_eligible_invoices' AND "
    "policy_version = 1 AND preview_fingerprint IS NOT NULL AND "
    "idempotency_key IS NOT NULL AND channel IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_topup_intents_account_credit_contract",
        "topup_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_topup_intents_account_credit_contract",
        "topup_intents",
        _CONTRACT,
    )
    op.create_index(
        "uq_topup_intents_period_purchase_idempotency",
        "topup_intents",
        ["account_id", "purpose", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'prepaid_period_purchase' AND idempotency_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_topup_intents_period_purchase_idempotency",
        table_name="topup_intents",
    )
    op.drop_constraint(
        "ck_topup_intents_account_credit_contract",
        "topup_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_topup_intents_account_credit_contract",
        "topup_intents",
        _OLD_CONTRACT,
    )
