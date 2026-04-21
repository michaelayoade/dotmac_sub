"""add correlation and cached result fields to service_port_allocations

Revision ID: 048_add_service_port_allocation_correlation
Revises: 047_add_provisioning_step_executions
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "048_add_service_port_allocation_correlation"
down_revision = "047_add_provisioning_step_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("service_port_allocations")
    }

    if "correlation_key" not in columns:
        op.add_column(
            "service_port_allocations",
            sa.Column("correlation_key", sa.String(length=256), nullable=True),
        )
    if "result_payload" not in columns:
        op.add_column(
            "service_port_allocations",
            sa.Column(
                "result_payload",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )

    existing_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("service_port_allocations")
    }
    if "uq_service_port_allocations_correlation_key" not in existing_uniques:
        op.create_unique_constraint(
            "uq_service_port_allocations_correlation_key",
            "service_port_allocations",
            ["correlation_key"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("service_port_allocations")
    }
    existing_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("service_port_allocations")
    }

    if "uq_service_port_allocations_correlation_key" in existing_uniques:
        op.drop_constraint(
            "uq_service_port_allocations_correlation_key",
            "service_port_allocations",
            type_="unique",
        )
    if "result_payload" in columns:
        op.drop_column("service_port_allocations", "result_payload")
    if "correlation_key" in columns:
        op.drop_column("service_port_allocations", "correlation_key")
