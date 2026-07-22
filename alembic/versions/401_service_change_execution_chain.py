"""Add canonical service-change execution evidence links.

Revision ID: 401_service_change_execution_chain
Revises: 400_subscription_relocation_intent
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "401_service_change_execution_chain"
down_revision = "400_subscription_relocation_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execution_state = sa.Enum(
        "awaiting_payment",
        "payment_settled",
        "fulfillment_released",
        "provisioning",
        "provisioning_verified",
        "completed",
        "failed",
        name="subscriptionchangeexecutionstate",
    )
    execution_state.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "subscription_change_requests",
        sa.Column("execution_state", execution_state, nullable=True),
    )
    foreign_keys = {
        "field_fee_invoice_id": ("invoices", "field_invoice"),
        "field_fee_payment_id": ("payments", "field_payment"),
        "service_order_id": ("service_orders", "service_order"),
        "work_order_id": ("work_order", "work_order"),
        "provisioning_readiness_decision_id": (
            "provisioning_readiness_decisions",
            "readiness_decision",
        ),
    }
    for column, (target, constraint_key) in foreign_keys.items():
        op.add_column(
            "subscription_change_requests",
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_unique_constraint(
            f"uq_sub_change_{constraint_key}",
            "subscription_change_requests",
            [column],
        )
        op.create_foreign_key(
            f"fk_sub_change_{constraint_key}",
            "subscription_change_requests",
            target,
            [column],
            ["id"],
            ondelete="RESTRICT",
        )
    op.add_column(
        "subscription_change_requests",
        sa.Column("payment_settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscription_change_requests",
        sa.Column(
            "provisioning_verified_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.execute(
        """
        UPDATE subscription_change_requests
        SET execution_state = CASE
            WHEN confirmation_snapshot ->> 'delivery_state' = 'awaiting_payment'
                THEN 'awaiting_payment'::subscriptionchangeexecutionstate
            WHEN confirmation_snapshot ->> 'delivery_state' = 'awaiting_verification'
                THEN 'payment_settled'::subscriptionchangeexecutionstate
            ELSE NULL
        END
        WHERE execution_state IS NULL
          AND confirmation_snapshot ->> 'delivery_mode' IN
              ('field_migration', 'remote_reprovision')
        """
    )


def downgrade() -> None:
    constraint_keys = {
        "provisioning_readiness_decision_id": "readiness_decision",
        "work_order_id": "work_order",
        "service_order_id": "service_order",
        "field_fee_payment_id": "field_payment",
        "field_fee_invoice_id": "field_invoice",
    }
    for column, constraint_key in constraint_keys.items():
        op.drop_constraint(
            f"fk_sub_change_{constraint_key}",
            "subscription_change_requests",
            type_="foreignkey",
        )
        op.drop_constraint(
            f"uq_sub_change_{constraint_key}",
            "subscription_change_requests",
            type_="unique",
        )
    for column in (
        "provisioning_verified_at",
        "payment_settled_at",
        "provisioning_readiness_decision_id",
        "work_order_id",
        "service_order_id",
        "field_fee_payment_id",
        "field_fee_invoice_id",
        "execution_state",
    ):
        op.drop_column("subscription_change_requests", column)
    sa.Enum(name="subscriptionchangeexecutionstate").drop(
        op.get_bind(), checkfirst=True
    )
