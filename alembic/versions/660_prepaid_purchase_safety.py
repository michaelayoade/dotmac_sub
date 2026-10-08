"""Merge current Sub history and preserve purchase-specific funding reservations.

Revision ID: 646_prepaid_purchase_safety
Revises: 642_network_map_import_feature_classification, 637_prepaid_period_purchase_intent_contract
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "646_prepaid_purchase_safety"
down_revision = (
    "642_network_map_import_feature_classification",
    "637_prepaid_period_purchase_intent_contract",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Refuse ambiguous existing live checkouts; operators must resolve them
    # without discarding potentially captured money before enabling the flag.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT subscription_id FROM prepaid_period_purchases "
                "WHERE status IN ('quoted', 'payment_pending') "
                "GROUP BY subscription_id HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicates is not None:
        raise RuntimeError("Resolve duplicate live period purchases before migration")
    op.add_column(
        "payments",
        sa.Column(
            "reserved_for_purchase_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "prepaid_period_purchases",
        sa.Column("verified_paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outage_compensation_decisions",
        sa.Column(
            "resolved_by_decision_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_outage_compensation_review_resolution",
        "outage_compensation_decisions",
        "outage_compensation_decisions",
        ["resolved_by_decision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_payments_reserved_for_purchase",
        "payments",
        "prepaid_period_purchases",
        ["reserved_for_purchase_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_payments_reserved_for_purchase_id", "payments", ["reserved_for_purchase_id"]
    )
    op.execute(
        sa.text(
            "UPDATE payments SET reserved_for_purchase_id = purchases.id "
            "FROM prepaid_period_purchases AS purchases "
            "WHERE purchases.payment_id = payments.id"
        )
    )
    op.create_index(
        "uq_prepaid_period_purchase_live_subscription",
        "prepaid_period_purchases",
        ["subscription_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('quoted', 'payment_pending')"),
    )


def downgrade() -> None:
    # Removing this guard could turn held customer receipts into spendable cash.
    held = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id FROM prepaid_period_purchases WHERE status = 'review_required' LIMIT 1"
            )
        )
        .first()
    )
    if held is not None:
        raise RuntimeError("Resolve held purchase receipts before downgrading")
    unallocated = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT payments.id FROM payments LEFT JOIN payment_settlements AS settled "
                "ON settled.payment_id = payments.id "
                "WHERE payments.reserved_for_purchase_id IS NOT NULL "
                "AND payments.is_active AND payments.status IN ('succeeded', 'partially_refunded') "
                "AND (settled.payment_id IS NULL OR settled.unallocated_amount - COALESCE(payments.refunded_amount, 0) > "
                "COALESCE((SELECT SUM(entries.amount) FROM payment_allocations AS allocation "
                "JOIN ledger_entries AS entries ON entries.id = allocation.consumption_ledger_entry_id "
                "WHERE allocation.payment_id = payments.id AND allocation.is_active AND entries.is_active), 0)) LIMIT 1"
            )
        )
        .first()
    )
    if unallocated is not None:
        raise RuntimeError(
            "Resolve unallocated reserved purchase receipts before downgrading"
        )
    op.drop_index(
        "uq_prepaid_period_purchase_live_subscription",
        table_name="prepaid_period_purchases",
    )
    op.drop_index("ix_payments_reserved_for_purchase_id", table_name="payments")
    op.drop_constraint(
        "fk_payments_reserved_for_purchase", "payments", type_="foreignkey"
    )
    op.drop_column("payments", "reserved_for_purchase_id")
    op.drop_column("prepaid_period_purchases", "verified_paid_at")
    op.drop_constraint(
        "fk_outage_compensation_review_resolution",
        "outage_compensation_decisions",
        type_="foreignkey",
    )
    op.drop_column("outage_compensation_decisions", "resolved_by_decision_id")
