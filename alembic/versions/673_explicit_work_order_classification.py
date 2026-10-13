"""Opt-in explicit work-order purpose and technology; legacy rows stay NULL.

Revision ID: 673_explicit_work_order_classification
Revises: 672_field_identity_review

Four nullable, default-free columns preserve legacy rows without backfill.
The constraint validates existing all-NULL rows in one bounded table scan.
Use the established migration budgets: lock_timeout 5s and statement_timeout
120s. A busy/large table fails atomically; retry the entire revision in an
approved maintenance window. Downgrade refuses evidence loss; forward fix only.
"""

import sqlalchemy as sa

from alembic import op

revision: str = "673_explicit_work_order_classification"
down_revision: str | None = "672_field_identity_review"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.add_column("work_order", sa.Column("purpose", sa.String(32), nullable=True))
    op.add_column(
        "work_order", sa.Column("access_technology", sa.String(16), nullable=True)
    )
    op.add_column(
        "work_order", sa.Column("template_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "work_order", sa.Column("classification_source", sa.String(40), nullable=True)
    )
    op.create_check_constraint(
        "ck_work_order_explicit_classification",
        "work_order",
        "(purpose IS NULL AND access_technology IS NULL AND template_version IS NULL AND classification_source IS NULL) OR "
        "(purpose IS NOT NULL AND access_technology IS NOT NULL AND template_version IS NOT NULL AND classification_source IS NOT NULL AND "
        "purpose IN ('repair', 'maintenance', 'customer_deployment', 'infrastructure_rollout', 'upgrade', 'relocation') AND "
        "access_technology IN ('fiber', 'radio', 'unknown') AND template_version = 1 AND "
        "classification_source = 'explicit_operator_selection' AND "
        "work_order_kind IN ('customer', 'infrastructure') AND "
        "(purpose != 'customer_deployment' OR work_order_kind = 'customer') AND "
        "(purpose != 'infrastructure_rollout' OR work_order_kind = 'infrastructure') AND "
        "(access_technology != 'unknown' OR purpose IN ('repair', 'maintenance')))",
    )


def downgrade() -> None:
    # Classified operational evidence is intentionally retained. A downgrade
    # requires an explicit data-retention decision and forward repair.
    raise RuntimeError("Explicit work-order classification requires a forward fix")
