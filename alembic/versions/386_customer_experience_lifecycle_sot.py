"""Cut customer-experience work relationships over to native Sub keys.

Revision ID: 386_customer_experience_lifecycle_sot
Revises: 385_rbac_catalog_normalized_identity

Project tasks previously carried one imported CRM work-order UUID.  Sub now
owns the complete work lifecycle, so the work-order root carries the native
``project_task_id`` foreign key instead.  This also makes the real cardinality
explicit: one project task may require zero or many field visits.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "386_customer_experience_lifecycle_sot"
down_revision = "385_rbac_catalog_normalized_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The read flip is complete and the retired controls have no remaining
    # runtime consumer. Historical import columns remain provenance only.
    op.execute(
        sa.text(
            """
            DELETE FROM domain_settings
             WHERE (domain::text = 'modules' AND key IN (
                        'projects_native_read',
                        'crm_work_order_pull'
                   ))
                OR (domain::text = 'projects' AND key = 'projects_native_read_enabled')
                OR (domain::text = 'scheduler' AND key = 'crm_work_order_pull_enabled')
            """
        )
    )
    op.add_column(
        "work_order",
        sa.Column("project_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # A retained imported link is valid only when its UUID resolves against the
    # work order's public identity.  Fail closed instead of dropping an
    # association that operators may still depend on.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM project_tasks AS task
                     WHERE task.work_order_id IS NOT NULL
                       AND NOT EXISTS (
                            SELECT 1
                              FROM work_order AS wo
                             WHERE wo.public_id = task.work_order_id::text
                       )
                ) THEN
                    RAISE EXCEPTION
                        'project task work-order cutover has unresolved imported links';
                END IF;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM project_tasks AS task
                      JOIN work_order AS wo
                        ON wo.public_id = task.work_order_id::text
                     WHERE task.work_order_id IS NOT NULL
                       AND wo.project_id IS NOT NULL
                       AND wo.project_id <> task.project_id
                ) THEN
                    RAISE EXCEPTION
                        'project task work-order cutover has conflicting project links';
                END IF;
            END $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE work_order AS wo
               SET project_id = task.project_id,
                   project_task_id = task.id
              FROM project_tasks AS task
             WHERE task.work_order_id IS NOT NULL
               AND wo.public_id = task.work_order_id::text
            """
        )
    )

    op.create_foreign_key(
        "fk_work_order_project_task_id_project_tasks",
        "work_order",
        "project_tasks",
        ["project_task_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_work_order_project_task_id",
        "work_order",
        ["project_task_id"],
    )
    op.drop_column("project_tasks", "work_order_id")

    # The native import retained CRM project UUIDs as Project.id. Refuse to
    # discard a mirror-only customer project or a conflicting owner.
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM project_mirror AS mirror
                     WHERE mirror.crm_project_id !~*
                               '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                        OR NOT EXISTS (
                            SELECT 1
                              FROM projects AS project
                             WHERE project.id = mirror.crm_project_id::uuid
                               AND project.subscriber_id = mirror.subscriber_id
                        )
                ) THEN
                    RAISE EXCEPTION
                        'project mirror retirement has unresolved customer projects';
                END IF;
            END $$
            """
        )
    )
    op.drop_table("project_sync_state")
    op.drop_table("project_mirror")
    op.drop_table("work_order_sync_state")


def downgrade() -> None:
    raise RuntimeError("customer-experience relationship cutover is irreversible")
