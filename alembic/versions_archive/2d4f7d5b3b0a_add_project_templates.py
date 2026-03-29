"""add project templates

Revision ID: 2d4f7d5b3b0a
Revises: c7d8a9b0e1f2
Create Date: 2026-01-14 00:00:00.000000
"""

from datetime import datetime, timezone
import uuid
from typing import Any, cast

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, ENUM


# revision identifiers, used by Alembic.
revision = "2d4f7d5b3b0a"
down_revision = "c7d8a9b0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "project_templates" not in existing_tables:
        op.create_table(
            "project_templates",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("project_type", ENUM(
                "cable_rerun",
                "fiber_optics_relocation",
                "radio_fiber_relocation",
                "fiber_optics_installation",
                "radio_installation",
                name="projecttype",
                create_type=False,
            ), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("project_type", name="uq_project_templates_project_type"),
        )

    if "project_template_tasks" not in existing_tables:
        op.create_table(
            "project_template_tasks",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("template_id", UUID(as_uuid=True), sa.ForeignKey("project_templates.id"), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", ENUM(
                "backlog",
                "todo",
                "in_progress",
                "blocked",
                "done",
                "canceled",
                name="project_taskstatus",
                create_type=False,
            ), nullable=True),
            sa.Column("priority", ENUM(
                "low",
                "normal",
                "high",
                "urgent",
                name="taskpriority",
                create_type=False,
            ), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    project_columns = {col["name"] for col in inspector.get_columns("projects")}
    if "project_template_id" not in project_columns:
        op.add_column(
            "projects",
            sa.Column("project_template_id", UUID(as_uuid=True), nullable=True),
        )
    project_fks = {fk["name"] for fk in inspector.get_foreign_keys("projects")}
    if "fk_projects_project_template_id" not in project_fks:
        op.create_foreign_key(
            "fk_projects_project_template_id",
            "projects",
            "project_templates",
            ["project_template_id"],
            ["id"],
        )

    task_columns = {col["name"] for col in inspector.get_columns("project_tasks")}
    if "template_task_id" not in task_columns:
        op.add_column(
            "project_tasks",
            sa.Column("template_task_id", UUID(as_uuid=True), nullable=True),
        )
    task_fks = {fk["name"] for fk in inspector.get_foreign_keys("project_tasks")}
    if "fk_project_tasks_template_task_id" not in task_fks:
        op.create_foreign_key(
            "fk_project_tasks_template_task_id",
            "project_tasks",
            "project_template_tasks",
            ["template_task_id"],
            ["id"],
        )

    has_templates = bind.execute(sa.text("SELECT 1 FROM project_templates LIMIT 1")).first()
    if not has_templates:
        now = datetime.now(timezone.utc)
        templates_table = sa.table(
            "project_templates",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("name", sa.String()),
            sa.column("project_type", ENUM(name="projecttype", create_type=False)),
            sa.column("description", sa.Text()),
            sa.column("is_active", sa.Boolean()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        tasks_table = sa.table(
            "project_template_tasks",
            sa.column("id", UUID(as_uuid=True)),
            sa.column("template_id", UUID(as_uuid=True)),
            sa.column("title", sa.String()),
            sa.column("description", sa.Text()),
            sa.column("status", ENUM(name="project_taskstatus", create_type=False)),
            sa.column("priority", ENUM(name="taskpriority", create_type=False)),
            sa.column("sort_order", sa.Integer()),
            sa.column("is_active", sa.Boolean()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )

        template_defs = [
            {
                "id": uuid.uuid4(),
                "name": "Fiber Optics Installation",
                "project_type": "fiber_optics_installation",
                "tasks": [
                    "Project Plan",
                    "Survey report",
                    "Drop Cable Installation",
                    "Lastmile Installation",
                    "Project Inspection",
                    "Power Direction",
                    "Activation",
                ],
            },
            {
                "id": uuid.uuid4(),
                "name": "Cable Rerun",
                "project_type": "cable_rerun",
                "tasks": [
                    "Survey report (Rerun)",
                    "Lastmile Installation (Rerun)",
                    "Integration",
                ],
            },
            {
                "id": uuid.uuid4(),
                "name": "Air Fiber Installation",
                "project_type": "radio_installation",
                "tasks": ["Customer Premise/Radio Installation"],
            },
            {
                "id": uuid.uuid4(),
                "name": "Air Fiber Relocation",
                "project_type": "radio_fiber_relocation",
                "tasks": ["Customer Premise/Radio Installation"],
            },
            {
                "id": uuid.uuid4(),
                "name": "Fiber Optics Relocation",
                "project_type": "fiber_optics_relocation",
                "tasks": [
                    "Project Plan",
                    "Survey report",
                    "Drop Cable Installation",
                    "Lastmile Installation",
                    "Project Inspection",
                    "Power Direction",
                    "Activation",
                ],
            },
        ]

        op.bulk_insert(
            templates_table,
            [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "project_type": item["project_type"],
                    "description": None,
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
                for item in template_defs
            ],
        )

        task_rows: list[dict[str, Any]] = []
        for template in template_defs:
            tasks = cast(list[str], template["tasks"])
            for idx, title in enumerate(tasks, start=1):
                task_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "template_id": template["id"],
                        "title": title,
                        "description": None,
                        "status": None,
                        "priority": None,
                        "sort_order": idx,
                        "is_active": True,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
        op.bulk_insert(tasks_table, task_rows)


def downgrade() -> None:
    op.drop_constraint(
        "fk_project_tasks_template_task_id",
        "project_tasks",
        type_="foreignkey",
    )
    op.drop_column("project_tasks", "template_task_id")

    op.drop_constraint(
        "fk_projects_project_template_id",
        "projects",
        type_="foreignkey",
    )
    op.drop_column("projects", "project_template_id")

    op.drop_table("project_template_tasks")
    op.drop_table("project_templates")
