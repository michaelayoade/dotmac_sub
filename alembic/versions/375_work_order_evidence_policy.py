"""Add work-order-owned as-built evidence policy.

Revision ID: 375_work_order_evidence_policy
Revises: 374_as_built_review_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "375_work_order_evidence_policy"
down_revision = "374_as_built_review_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_order",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "work_order",
        sa.Column(
            "requires_as_built_evidence",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_work_order_project_id_projects",
        "work_order",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_work_order_project_id",
        "work_order",
        ["project_id"],
    )
    # Imported CRM project ids are the same UUIDs retained by native projects.
    # Resolve only exact existing matches; do not infer from names or metadata.
    op.execute(
        """
        UPDATE work_order AS wo
        SET project_id = project.id
        FROM projects AS project
        WHERE wo.project_id IS NULL
          AND wo.crm_project_id = project.id::text
        """
    )
    op.add_column(
        "installation_project_lifecycle_events",
        sa.Column("decision_context", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("installation_project_lifecycle_events", "decision_context")
    op.drop_index("ix_work_order_project_id", table_name="work_order")
    op.drop_constraint(
        "fk_work_order_project_id_projects",
        "work_order",
        type_="foreignkey",
    )
    op.drop_column("work_order", "requires_as_built_evidence")
    op.drop_column("work_order", "project_id")
