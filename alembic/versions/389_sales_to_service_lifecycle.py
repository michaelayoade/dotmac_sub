"""Connect sales, implementation, provisioning, and CX lifecycle roots.

Revision ID: 389_sales_to_service_lifecycle
Revises: 388_device_projection_class_facts
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "389_sales_to_service_lifecycle"
down_revision = "388_device_projection_class_facts"
branch_labels = None
depends_on = None


def _inspector():
    # PostgreSQL inspectors cache schema state, so create a fresh one after
    # each structural operation. This migration must work both after the
    # historical chain and after 001_squashed creates the current models.
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return any(item["name"] == column for item in _inspector().get_columns(table))


def _has_foreign_key(table: str, columns: list[str]) -> bool:
    return any(
        item.get("constrained_columns") == columns
        for item in _inspector().get_foreign_keys(table)
    )


def _has_unique(table: str, columns: list[str]) -> bool:
    inspector = _inspector()
    return any(
        item.get("column_names") == columns
        for item in inspector.get_unique_constraints(table)
    ) or any(
        item.get("unique") and item.get("column_names") == columns
        for item in inspector.get_indexes(table)
    )


def _index(table: str, name: str) -> dict | None:
    return next(
        (item for item in _inspector().get_indexes(table) if item.get("name") == name),
        None,
    )


def upgrade() -> None:
    if not _has_column("lead_origin_captures", "integration_inbox_id"):
        op.add_column(
            "lead_origin_captures",
            sa.Column(
                "integration_inbox_id", postgresql.UUID(as_uuid=True), nullable=True
            ),
        )
    if not _has_column("lead_origin_captures", "source_interaction_id"):
        op.add_column(
            "lead_origin_captures",
            sa.Column("source_interaction_id", sa.String(length=240), nullable=True),
        )
    if not _has_column("lead_origin_captures", "capture_fingerprint"):
        op.add_column(
            "lead_origin_captures",
            sa.Column("capture_fingerprint", sa.String(length=64), nullable=True),
        )
    if not _has_foreign_key("lead_origin_captures", ["integration_inbox_id"]):
        op.create_foreign_key(
            "fk_lead_origin_captures_integration_inbox",
            "lead_origin_captures",
            "integration_inbox",
            ["integration_inbox_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _has_unique("lead_origin_captures", ["integration_inbox_id"]):
        op.create_unique_constraint(
            "uq_lead_origin_captures_integration_inbox",
            "lead_origin_captures",
            ["integration_inbox_id"],
        )
    if (
        _index("lead_origin_captures", "uq_lead_origin_captures_source_interaction")
        is None
    ):
        op.create_index(
            "uq_lead_origin_captures_source_interaction",
            "lead_origin_captures",
            ["source_platform", "source_interaction_id"],
            unique=True,
            postgresql_where=sa.text("source_interaction_id IS NOT NULL"),
        )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION lead_origin_captures_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'lead origin evidence is append-only';
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS lead_origin_captures_append_only
                ON lead_origin_captures;
            CREATE TRIGGER lead_origin_captures_append_only
            BEFORE UPDATE OR DELETE ON lead_origin_captures
            FOR EACH ROW EXECUTE FUNCTION lead_origin_captures_append_only();
            """
        )
    )

    if not _has_column("projects", "quote_id"):
        op.add_column(
            "projects",
            sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_column("projects", "sales_order_id"):
        op.add_column(
            "projects",
            sa.Column("sales_order_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_foreign_key("projects", ["quote_id"]):
        op.create_foreign_key(
            "fk_projects_quote_id",
            "projects",
            "quotes",
            ["quote_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _has_foreign_key("projects", ["sales_order_id"]):
        op.create_foreign_key(
            "fk_projects_sales_order_id",
            "projects",
            "sales_orders",
            ["sales_order_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _has_unique("projects", ["quote_id"]):
        op.create_unique_constraint("uq_projects_quote_id", "projects", ["quote_id"])
    if not _has_unique("projects", ["sales_order_id"]):
        op.create_unique_constraint(
            "uq_projects_sales_order_id", "projects", ["sales_order_id"]
        )
    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT p.id,
                       CASE WHEN p.metadata ->> 'quote_id' ~*
                           '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                       THEN (p.metadata ->> 'quote_id')::uuid END AS quote_id
                  FROM projects AS p
            ), ranked AS (
                SELECT candidate.id, candidate.quote_id,
                       row_number() OVER (
                           PARTITION BY candidate.quote_id ORDER BY p.created_at, p.id
                       ) AS position
                  FROM candidates AS candidate
                  JOIN projects AS p ON p.id = candidate.id
                  JOIN quotes AS q ON q.id = candidate.quote_id
                 WHERE candidate.quote_id IS NOT NULL
            )
            UPDATE projects AS p
               SET quote_id = ranked.quote_id
              FROM ranked
             WHERE p.id = ranked.id AND ranked.position = 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT p.id,
                       CASE WHEN p.metadata ->> 'sales_order_id' ~*
                           '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                       THEN (p.metadata ->> 'sales_order_id')::uuid END AS sales_order_id
                  FROM projects AS p
            ), ranked AS (
                SELECT candidate.id, candidate.sales_order_id,
                       row_number() OVER (
                           PARTITION BY candidate.sales_order_id ORDER BY p.created_at, p.id
                       ) AS position
                  FROM candidates AS candidate
                  JOIN projects AS p ON p.id = candidate.id
                  JOIN sales_orders AS so ON so.id = candidate.sales_order_id
                 WHERE candidate.sales_order_id IS NOT NULL
            )
            UPDATE projects AS p
               SET sales_order_id = ranked.sales_order_id
              FROM ranked
             WHERE p.id = ranked.id AND ranked.position = 1
            """
        )
    )

    for name, table in (
        ("project_id", "projects"),
        ("installation_project_id", "installation_projects"),
    ):
        if not _has_column("service_orders", name):
            op.add_column(
                "service_orders",
                sa.Column(name, postgresql.UUID(as_uuid=True), nullable=True),
            )
        if not _has_foreign_key("service_orders", [name]):
            op.create_foreign_key(
                f"fk_service_orders_{name}",
                "service_orders",
                table,
                [name],
                ["id"],
                ondelete="RESTRICT",
            )
        if _index("service_orders", f"ix_service_orders_{name}") is None:
            op.create_index(f"ix_service_orders_{name}", "service_orders", [name])
    if not _has_column("service_orders", "idempotency_key"):
        op.add_column(
            "service_orders",
            sa.Column("idempotency_key", sa.String(length=240), nullable=True),
        )
    if not _has_column("service_orders", "implementation_verified_at"):
        op.add_column(
            "service_orders",
            sa.Column(
                "implementation_verified_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
    if not _has_column("service_orders", "implementation_verification_event_id"):
        op.add_column(
            "service_orders",
            sa.Column(
                "implementation_verification_event_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )
    if not _has_unique("service_orders", ["implementation_verification_event_id"]):
        op.create_unique_constraint(
            "uq_service_orders_implementation_event",
            "service_orders",
            ["implementation_verification_event_id"],
        )
    idempotency_index = _index("service_orders", "ix_service_orders_idempotency_key")
    if idempotency_index is not None and not idempotency_index.get("unique"):
        op.drop_index("ix_service_orders_idempotency_key", table_name="service_orders")
        idempotency_index = None
    if idempotency_index is None:
        op.create_index(
            "ix_service_orders_idempotency_key",
            "service_orders",
            ["idempotency_key"],
            unique=True,
        )
    op.execute(
        sa.text(
            """
            UPDATE service_orders AS service_order
               SET idempotency_key = 'sales-order-line:' || service_order.sales_order_line_id,
                   project_id = project.id,
                   installation_project_id = installation.id
              FROM projects AS project
              LEFT JOIN installation_projects AS installation
                ON installation.project_id = project.id
             WHERE service_order.sales_order_line_id IS NOT NULL
               AND project.sales_order_id = service_order.sales_order_id
            """
        )
    )

    if not _has_table("customer_experience_handoffs"):
        op.create_table(
            "customer_experience_handoffs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("sales_order_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "installation_project_id",
                postgresql.UUID(as_uuid=True),
                nullable=False,
            ),
            sa.Column(
                "service_order_id", postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("policy_version", sa.Integer(), nullable=False),
            sa.Column("readiness_evidence", sa.JSON(), nullable=False),
            sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_by_actor_type", sa.String(length=40), nullable=True),
            sa.Column("accepted_by_actor_id", sa.String(length=160), nullable=True),
            sa.Column("attention_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN "
                "('pending', 'ready', 'accepted', 'needs_attention', 'canceled')",
                name="ck_cx_handoffs_status",
            ),
            sa.CheckConstraint(
                "policy_version >= 1", name="ck_cx_handoffs_policy_version"
            ),
            sa.ForeignKeyConstraint(
                ["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["sales_order_id"], ["sales_orders.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["installation_project_id"],
                ["installation_projects.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["service_order_id"], ["service_orders.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("subscription_id", name="uq_cx_handoffs_subscription"),
            sa.UniqueConstraint(
                "service_order_id", name="uq_cx_handoffs_service_order"
            ),
        )
    if (
        _index("customer_experience_handoffs", "ix_cx_handoffs_subscriber_status")
        is None
    ):
        op.create_index(
            "ix_cx_handoffs_subscriber_status",
            "customer_experience_handoffs",
            ["subscriber_id", "status"],
        )
    if not _has_table("customer_experience_handoff_events"):
        op.create_table(
            "customer_experience_handoff_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("event_type", sa.String(length=100), nullable=False),
            sa.Column("from_status", sa.String(length=24), nullable=False),
            sa.Column("to_status", sa.String(length=24), nullable=False),
            sa.Column("actor_type", sa.String(length=40), nullable=False),
            sa.Column("actor_id", sa.String(length=160), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("decision_context", sa.JSON(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "from_status <> to_status", name="ck_cx_handoff_event_change"
            ),
            sa.ForeignKeyConstraint(
                ["handoff_id"],
                ["customer_experience_handoffs.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    for name, columns, unique in (
        ("ix_cx_handoff_events_event_id", ["event_id"], True),
        ("ix_cx_handoff_events_event_type", ["event_type"], False),
        ("ix_cx_handoff_events_actor_id", ["actor_id"], False),
        (
            "ix_cx_handoff_events_handoff_occurred",
            ["handoff_id", "occurred_at"],
            False,
        ),
    ):
        if _index("customer_experience_handoff_events", name) is None:
            op.create_index(
                name,
                "customer_experience_handoff_events",
                columns,
                unique=unique,
            )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION customer_experience_handoff_events_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'customer-experience handoff evidence is append-only';
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS customer_experience_handoff_events_append_only
                ON customer_experience_handoff_events;
            CREATE TRIGGER customer_experience_handoff_events_append_only
            BEFORE UPDATE OR DELETE ON customer_experience_handoff_events
            FOR EACH ROW EXECUTE FUNCTION customer_experience_handoff_events_append_only();
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("sales-to-service lifecycle cutover is irreversible")
