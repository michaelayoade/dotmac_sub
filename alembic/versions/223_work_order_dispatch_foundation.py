"""Work-order Phase 2 dispatch foundation tables.

Revision ID: 223_work_order_dispatch_foundation
Revises: 222_work_order_phase2_schema_parity
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "223_work_order_dispatch_foundation"
down_revision = "222_work_order_phase2_schema_parity"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return _inspector().has_table(name)


def upgrade() -> None:
    if not _has_table("skills"):
        op.create_table(
            "skills",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("name", name="uq_skills_name"),
        )

    if not _has_table("technician_profiles"):
        op.create_table(
            "technician_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("system_user_id", postgresql.UUID(as_uuid=True)),
            sa.Column("crm_person_id", sa.String(length=64)),
            sa.Column("title", sa.String(length=120)),
            sa.Column("region", sa.String(length=120)),
            sa.Column("erp_employee_id", sa.String(length=100)),
            sa.Column("metadata", sa.JSON()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["system_user_id"], ["system_users.id"]),
            sa.UniqueConstraint("system_user_id", name="uq_technician_profiles_system_user_id"),
            sa.UniqueConstraint("crm_person_id", name="uq_technician_profiles_crm_person_id"),
            sa.UniqueConstraint("erp_employee_id", name="uq_technician_profiles_erp_employee_id"),
        )
        op.create_index(
            "ix_technician_profiles_system_user_id",
            "technician_profiles",
            ["system_user_id"],
        )
        op.create_index(
            "ix_technician_profiles_crm_person_id",
            "technician_profiles",
            ["crm_person_id"],
        )
        op.create_index(
            "ix_technician_profiles_erp_employee_id",
            "technician_profiles",
            ["erp_employee_id"],
        )
        op.create_index("ix_technician_profiles_region", "technician_profiles", ["region"])

    if not _has_table("technician_skills"):
        op.create_table(
            "technician_skills",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("technician_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("proficiency", sa.Integer()),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["technician_id"], ["technician_profiles.id"]),
            sa.ForeignKeyConstraint(["skill_id"], ["skills.id"]),
            sa.UniqueConstraint("technician_id", "skill_id", name="uq_technician_skill"),
        )

    if not _has_table("shifts"):
        op.create_table(
            "shifts",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("technician_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("timezone", sa.String(length=64)),
            sa.Column("shift_type", sa.String(length=60)),
            sa.Column("erp_id", sa.String(length=100)),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["technician_id"], ["technician_profiles.id"]),
            sa.UniqueConstraint("erp_id", name="uq_shifts_erp_id"),
        )
        op.create_index("ix_shifts_erp_id", "shifts", ["erp_id"])

    if not _has_table("availability_blocks"):
        op.create_table(
            "availability_blocks",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("technician_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(length=160)),
            sa.Column("block_type", sa.String(length=60)),
            sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("erp_id", sa.String(length=100)),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["technician_id"], ["technician_profiles.id"]),
            sa.UniqueConstraint("erp_id", name="uq_availability_blocks_erp_id"),
        )
        op.create_index("ix_availability_blocks_erp_id", "availability_blocks", ["erp_id"])

    if not _has_table("dispatch_rules"):
        op.create_table(
            "dispatch_rules",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("work_type", sa.String(length=40)),
            sa.Column("work_priority", sa.String(length=40)),
            sa.Column("region", sa.String(length=120)),
            sa.Column("service_team_id", postgresql.UUID(as_uuid=True)),
            sa.Column("skill_ids", sa.JSON()),
            sa.Column("auto_assign", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["service_team_id"], ["service_teams.id"]),
        )
        op.create_index(
            "ix_dispatch_rules_active_priority",
            "dispatch_rules",
            ["is_active", "priority"],
        )

    if not _has_table("work_order_assignment_queue"):
        op.create_table(
            "work_order_assignment_queue",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("work_order_mirror_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("crm_work_order_id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
            sa.Column("reason", sa.Text()),
            sa.Column("dispatch_rule_id", postgresql.UUID(as_uuid=True)),
            sa.Column("assigned_technician_id", postgresql.UUID(as_uuid=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["work_order_mirror_id"], ["work_order_mirror.id"]),
            sa.ForeignKeyConstraint(["dispatch_rule_id"], ["dispatch_rules.id"]),
            sa.ForeignKeyConstraint(["assigned_technician_id"], ["technician_profiles.id"]),
        )
        op.create_index(
            "ix_work_order_assignment_queue_crm_work_order_id",
            "work_order_assignment_queue",
            ["crm_work_order_id"],
        )
        op.create_index(
            "ix_work_order_assignment_queue_status_created",
            "work_order_assignment_queue",
            ["status", "created_at"],
        )


def downgrade() -> None:
    for table_name in (
        "work_order_assignment_queue",
        "dispatch_rules",
        "availability_blocks",
        "shifts",
        "technician_skills",
        "technician_profiles",
        "skills",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
