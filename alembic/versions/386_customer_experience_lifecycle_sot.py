"""Cut customer-experience work relationships over to native Sub keys.

Revision ID: 386_customer_experience_lifecycle_sot
Revises: 385_rbac_catalog_normalized_identity

Project tasks previously carried one imported CRM work-order UUID.  Sub now
owns the complete work lifecycle, so the work-order root carries the native
``project_task_id`` foreign key instead.  This also makes the real cardinality
explicit: one project task may require zero or many field visits.

A fresh ``001_squashed`` DB is built from current models, which already carry
``work_order.project_task_id`` and no longer declare the imported link or the
mirror tables — every structural step guards on what is actually present.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "386_customer_experience_lifecycle_sot"
down_revision = "385_rbac_catalog_normalized_identity"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

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

    if not _has_column(bind, "work_order", "project_task_id"):
        op.add_column(
            "work_order",
            sa.Column("project_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

    if _has_column(bind, "project_tasks", "work_order_id"):
        # A retained imported link is valid only when its UUID resolves against
        # the work order's public identity.  Fail closed instead of dropping an
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

    inspector = sa.inspect(bind)
    fk_names = {fk["name"] for fk in inspector.get_foreign_keys("work_order")}
    if "fk_work_order_project_task_id_project_tasks" not in fk_names and not any(
        fk["constrained_columns"] == ["project_task_id"]
        for fk in inspector.get_foreign_keys("work_order")
    ):
        op.create_foreign_key(
            "fk_work_order_project_task_id_project_tasks",
            "work_order",
            "project_tasks",
            ["project_task_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    index_names = {ix["name"] for ix in inspector.get_indexes("work_order")}
    if not index_names.intersection(
        {"ix_work_order_project_task_id", "ix_work_order_project_task_id_1"}
    ) and not any(
        ix["column_names"] == ["project_task_id"]
        for ix in inspector.get_indexes("work_order")
    ):
        op.create_index(
            "ix_work_order_project_task_id",
            "work_order",
            ["project_task_id"],
        )

    if _has_column(bind, "project_tasks", "work_order_id"):
        op.drop_column("project_tasks", "work_order_id")

    if "project_mirror" in tables:
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
    if "project_sync_state" in tables:
        op.drop_table("project_sync_state")
    if "project_mirror" in tables:
        op.drop_table("project_mirror")
    if "work_order_sync_state" in tables:
        op.drop_table("work_order_sync_state")


def downgrade() -> None:
    raise RuntimeError("customer-experience relationship cutover is irreversible")
