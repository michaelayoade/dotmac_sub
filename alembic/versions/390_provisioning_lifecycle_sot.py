"""Cut provisioning readiness and activation over to one Sub owner.

Revision ID: 390_provisioning_lifecycle_sot
Revises: 389_sales_to_service_lifecycle
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "390_provisioning_lifecycle_sot"
down_revision = "389_sales_to_service_lifecycle"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return any(item["name"] == column for item in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if not _has_column(bind, "service_orders", "project_id"):
        op.add_column(
            "service_orders",
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_service_orders_project_id",
            "service_orders",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            "ix_service_orders_project_id", "service_orders", ["project_id"]
        )

    if not _has_column(bind, "service_orders", "activation_project_task_id"):
        op.add_column(
            "service_orders",
            sa.Column(
                "activation_project_task_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
        op.create_foreign_key(
            "fk_service_orders_activation_project_task_id",
            "service_orders",
            "project_tasks",
            ["activation_project_task_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            "ix_service_orders_activation_project_task_id",
            "service_orders",
            ["activation_project_task_id"],
        )

    if bind.dialect.name == "postgresql":
        # Backfill only unambiguous native relationships. Any unresolved order
        # remains visibly blocked by the readiness owner instead of guessing.
        op.execute(
            sa.text(
                """
                WITH matches AS (
                    SELECT so.id AS service_order_id,
                           min(project.id::text)::uuid AS project_id,
                           count(*) AS match_count
                      FROM service_orders AS so
                      JOIN projects AS project
                        ON project.is_active
                       AND project.subscriber_id = so.subscriber_id
                       AND project.metadata ->> 'sales_order_id' = so.sales_order_id::text
                     WHERE so.project_id IS NULL
                       AND so.sales_order_id IS NOT NULL
                     GROUP BY so.id
                )
                UPDATE service_orders AS so
                   SET project_id = matches.project_id
                  FROM matches
                 WHERE so.id = matches.service_order_id
                   AND matches.match_count = 1
                """
            )
        )
        op.execute(
            sa.text(
                """
                WITH matches AS (
                    SELECT so.id AS service_order_id,
                           min(project.id::text)::uuid AS project_id,
                           count(*) AS match_count
                      FROM service_orders AS so
                      JOIN sales_orders AS sales_order ON sales_order.id = so.sales_order_id
                      JOIN projects AS project
                        ON project.is_active
                       AND project.subscriber_id = so.subscriber_id
                       AND project.metadata ->> 'quote_id' = sales_order.quote_id::text
                     WHERE so.project_id IS NULL
                       AND sales_order.quote_id IS NOT NULL
                     GROUP BY so.id
                )
                UPDATE service_orders AS so
                   SET project_id = matches.project_id
                  FROM matches
                 WHERE so.id = matches.service_order_id
                   AND matches.match_count = 1
                """
            )
        )
        op.execute(
            sa.text(
                """
                WITH matches AS (
                    SELECT so.id AS service_order_id,
                           min(task.id::text)::uuid AS task_id,
                           count(*) AS match_count
                      FROM service_orders AS so
                      JOIN project_tasks AS task
                        ON task.project_id = so.project_id
                       AND task.is_active
                       AND task.metadata ->> 'fiber_stage_key' = 'power_splicing_activation'
                     WHERE so.activation_project_task_id IS NULL
                       AND so.project_id IS NOT NULL
                     GROUP BY so.id
                )
                UPDATE service_orders AS so
                   SET activation_project_task_id = matches.task_id
                  FROM matches
                 WHERE so.id = matches.service_order_id
                   AND matches.match_count = 1
                """
            )
        )

    decision_status = postgresql.ENUM(
        "blocked",
        "activation_requested",
        "activated",
        "failed",
        name="provisioning_readiness_decision_status",
        create_type=False,
    )
    check_kind = postgresql.ENUM(
        "provisioning_run",
        "project_binding",
        "activation_task",
        "field_work",
        "ip_assignment",
        name="provisioning_readiness_check_kind",
        create_type=False,
    )
    check_result = postgresql.ENUM(
        "passed",
        "failed",
        "not_applicable",
        name="provisioning_readiness_check_result",
        create_type=False,
    )
    if bind.dialect.name == "postgresql":
        decision_status.create(bind, checkfirst=True)
        check_kind.create(bind, checkfirst=True)
        check_result.create(bind, checkfirst=True)

    if "provisioning_readiness_decisions" not in tables:
        op.create_table(
            "provisioning_readiness_decisions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("service_order_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("provisioning_run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "status",
                decision_status if bind.dialect.name == "postgresql" else sa.String(32),
                nullable=False,
            ),
            sa.Column("reason_code", sa.String(length=80), nullable=False),
            sa.Column("actor", sa.String(length=160), nullable=False),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["provisioning_run_id"], ["provisioning_runs.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["service_order_id"], ["service_orders.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("command_id"),
        )
        op.create_index(
            "ix_provisioning_readiness_decisions_service_order_id",
            "provisioning_readiness_decisions",
            ["service_order_id"],
        )
        op.create_index(
            "ix_provisioning_readiness_decisions_provisioning_run_id",
            "provisioning_readiness_decisions",
            ["provisioning_run_id"],
        )
        op.create_index(
            "ix_provisioning_readiness_decisions_correlation_id",
            "provisioning_readiness_decisions",
            ["correlation_id"],
        )

    if "provisioning_readiness_checks" not in tables:
        op.create_table(
            "provisioning_readiness_checks",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "kind",
                check_kind if bind.dialect.name == "postgresql" else sa.String(32),
                nullable=False,
            ),
            sa.Column(
                "result",
                check_result if bind.dialect.name == "postgresql" else sa.String(32),
                nullable=False,
            ),
            sa.Column("reason_code", sa.String(length=80), nullable=False),
            sa.Column("source_type", sa.String(length=80), nullable=False),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["decision_id"],
                ["provisioning_readiness_decisions.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "decision_id", "kind", name="uq_provisioning_readiness_check_kind"
            ),
        )
        op.create_index(
            "ix_provisioning_readiness_checks_decision_id",
            "provisioning_readiness_checks",
            ["decision_id"],
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION provisioning_readiness_decisions_append_only()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'provisioning readiness evidence is append-only';
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS provisioning_readiness_decisions_append_only
                    ON provisioning_readiness_decisions;
                CREATE TRIGGER provisioning_readiness_decisions_append_only
                BEFORE UPDATE OR DELETE ON provisioning_readiness_decisions
                FOR EACH ROW EXECUTE FUNCTION
                    provisioning_readiness_decisions_append_only();
                """
            )
        )
        op.execute(
            sa.text(
                """
                CREATE OR REPLACE FUNCTION provisioning_readiness_checks_append_only()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'provisioning readiness evidence is append-only';
                END;
                $$ LANGUAGE plpgsql;

                DROP TRIGGER IF EXISTS provisioning_readiness_checks_append_only
                    ON provisioning_readiness_checks;
                CREATE TRIGGER provisioning_readiness_checks_append_only
                BEFORE UPDATE OR DELETE ON provisioning_readiness_checks
                FOR EACH ROW EXECUTE FUNCTION provisioning_readiness_checks_append_only();
                """
            )
        )


def downgrade() -> None:
    raise RuntimeError(
        "Provisioning lifecycle authority cutover is irreversible; restore from backup."
    )
