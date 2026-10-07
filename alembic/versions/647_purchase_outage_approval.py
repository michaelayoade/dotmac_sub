"""Merge current histories and retain approved, exact time-credit evidence."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "647_purchase_outage_approval"
down_revision = ("646_prepaid_purchase_safety", "646_test_connection_finance_review")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outage_compensation_decisions",
        sa.Column("approved_by", sa.UUID(), nullable=True),
    )
    op.add_column(
        "outage_compensation_decisions",
        sa.Column("approval_reason", sa.String(1000), nullable=True),
    )
    op.add_column(
        "outage_compensation_decisions",
        sa.Column("approved_fingerprint", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_outage_compensation_approved_by",
        "outage_compensation_decisions",
        "system_users",
        ["approved_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "compensated_service_times",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.UUID(),
            sa.ForeignKey("subscriptions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_kind",
            "source_id",
            "ordinal",
            name="uq_compensated_service_time_source",
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_compensated_service_time_positive"
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_compensated_service_time_ordinal"),
        sa.CheckConstraint(
            "source_kind IN ('pause', 'extension', 'outage')",
            name="ck_compensated_service_time_source",
        ),
    )
    op.create_index(
        "ix_compensated_service_time_subscription",
        "compensated_service_times",
        ["subscription_id", "starts_at"],
    )
    # Claims are accounting history. Recovery adds evidence, never edits/deletes it.
    op.execute("""CREATE FUNCTION refuse_time_credit_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Time credit evidence is append-only'; END $$""")
    op.execute(
        "CREATE TRIGGER compensated_time_append_only BEFORE UPDATE OR DELETE ON compensated_service_times FOR EACH ROW EXECUTE FUNCTION refuse_time_credit_mutation()"
    )
    op.execute("""INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at)
        VALUES (gen_random_uuid(), 'billing:outage_compensation:approve', 'Approve reviewed outage time compensation', true, true, now(), now())
        ON CONFLICT (key) DO NOTHING""")
    op.execute("""INSERT INTO role_permissions (id, role_id, permission_id)
        SELECT gen_random_uuid(), roles.id, permissions.id FROM roles CROSS JOIN permissions
        WHERE lower(trim(roles.name)) IN ('finance', 'finance_manager', 'admin')
          AND permissions.key = 'billing:outage_compensation:approve'
        ON CONFLICT (role_id, permission_id) DO NOTHING""")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM compensated_service_times) OR EXISTS(SELECT 1 FROM outage_compensation_decisions WHERE approved_by IS NOT NULL OR status = 'awaiting_approval')"
        )
    ):
        raise RuntimeError(
            "Approval or time-credit evidence exists; use a forward correction."
        )
    op.drop_table("compensated_service_times")
    op.execute("DROP FUNCTION refuse_time_credit_mutation()")
    op.drop_constraint(
        "fk_outage_compensation_approved_by",
        "outage_compensation_decisions",
        type_="foreignkey",
    )
    for name in ("approved_fingerprint", "approval_reason", "approved_by"):
        op.drop_column("outage_compensation_decisions", name)
    # Preserve the additive permission catalog and subsequent custom assignments.
