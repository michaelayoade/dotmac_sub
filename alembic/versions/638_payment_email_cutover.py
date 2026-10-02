"""Durable payment email correlation and explicit content cutover.

Revision ID: 638_payment_email_cutover
Revises: 637_comms_delivery_coverage
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "638_payment_email_cutover"
down_revision: str | None = "637_comms_delivery_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON public.{table} USING (tenant_id = app_current_tenant_id()) WITH CHECK (tenant_id = app_current_tenant_id())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO app_user")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    # These existing single-operator assembly tables are prerequisites of the
    # routing/content seal and its durable evidence writer. Supply the named
    # runtime privileges here rather than injecting them in a role canary.
    # This does not supply the rest of the estate's runtime grant cutover.
    op.execute("GRANT SELECT, UPDATE ON public.notification_templates TO app_user")
    op.execute("GRANT SELECT, INSERT ON public.event_store TO app_user")
    op.add_column(
        "notification_templates",
        sa.Column(
            "studio_content_sealed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "payment_email_episodes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("subscriber_id", sa.UUID(), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("notification_id", sa.UUID(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"], ["notifications.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "payment_id",
            "invoice_id",
            "recipient",
            name="uq_payment_email_episode_identity",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_payment_email_episode_scope"),
        sa.UniqueConstraint(
            "notification_id", name="uq_payment_email_episode_delivery"
        ),
        sa.CheckConstraint(
            "deadline_at <= created_at + interval '60 seconds' AND deadline_at >= created_at",
            name="ck_payment_email_collection_bound",
        ),
    )
    op.create_table(
        "payment_email_parts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("episode_id", sa.UUID(), nullable=False),
        sa.Column("source_event_id", sa.UUID(), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("content_template_id", sa.UUID(), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(200)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "episode_id"],
            ["payment_email_episodes.tenant_id", "payment_email_episodes.id"],
            ondelete="CASCADE",
            name="fk_payment_email_part_episode_scope",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "decision_id"],
            [
                "communication_intent_recipients.tenant_id",
                "communication_intent_recipients.id",
            ],
            ondelete="RESTRICT",
            name="fk_payment_email_part_decision_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_event_id",
            "recipient",
            name="uq_payment_email_part_source",
        ),
        sa.UniqueConstraint("episode_id", "kind", name="uq_payment_email_part_kind"),
        sa.UniqueConstraint("decision_id", name="uq_payment_email_part_decision"),
        sa.CheckConstraint(
            "kind IN ('receipt', 'invoice_paid')", name="ck_payment_email_part_kind"
        ),
        sa.CheckConstraint("length(trim(body)) > 0", name="ck_payment_email_part_body"),
        sa.CheckConstraint("content_version > 0", name="ck_payment_email_part_version"),
    )
    op.create_table(
        "payment_email_cutovers",
        sa.Column(
            "composition_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("receipt_legacy_id", sa.UUID(), nullable=False),
        sa.Column("invoice_legacy_id", sa.UUID(), nullable=False),
        sa.Column("receipt_content_id", sa.UUID(), nullable=False),
        sa.Column("invoice_content_id", sa.UUID(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.ForeignKeyConstraint(
            ["receipt_legacy_id"], ["notification_templates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_legacy_id"], ["notification_templates.id"], ondelete="RESTRICT"
        ),
    )
    for table in (
        "payment_email_episodes",
        "payment_email_parts",
        "payment_email_cutovers",
    ):
        # Sub's squashed base renders current public ORM metadata before the
        # operator-tenant provider (508). Like domain_settings (523), these
        # cross-base FKs are migration-owned and must be installed here even
        # when that base already materialized the application table.
        op.create_foreign_key(
            f"fk_{table}_tenant",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            source_schema="public",
            referent_schema="public",
            ondelete="CASCADE",
        )
        _rls(table)

    # The old content writer stays sealed even for callers outside the admin
    # editor. Routing policy (conditions/purpose/active) remains product-owned.
    op.execute("""
        CREATE FUNCTION public.prevent_sealed_payment_email_content()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' AND OLD.studio_content_sealed THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Activated payment email routing identity cannot be deleted';
                ELSIF ROW(OLD.code, OLD.channel, OLD.subject, OLD.body, OLD.studio_content_sealed)
                    IS DISTINCT FROM ROW(NEW.code, NEW.channel, NEW.subject, NEW.body, NEW.studio_content_sealed)
                THEN
                    RAISE EXCEPTION 'Payment email content is authored in Template Studio';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF NEW.channel::text = 'email'
                AND NEW.code IN ('payment_received', 'payment_received_email', 'invoice_paid', 'invoice_paid_email')
                AND (TG_OP = 'INSERT' OR ROW(OLD.code, OLD.channel) IS DISTINCT FROM ROW(NEW.code, NEW.channel))
                AND EXISTS (SELECT 1 FROM public.notification_templates WHERE studio_content_sealed)
            THEN
                RAISE EXCEPTION 'Activated payment email routing identity cannot be added or rebound';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER guard_sealed_payment_email_content
        BEFORE INSERT OR UPDATE OR DELETE ON public.notification_templates
        FOR EACH ROW EXECUTE FUNCTION public.prevent_sealed_payment_email_content()
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER guard_sealed_payment_email_content ON public.notification_templates"
    )
    op.execute("DROP FUNCTION public.prevent_sealed_payment_email_content()")
    op.drop_table("payment_email_cutovers")
    op.drop_table("payment_email_parts")
    op.drop_table("payment_email_episodes")
    op.drop_column("notification_templates", "studio_content_sealed")
