"""Add enforcement_locks table

Revision ID: a2de92d25263
Revises: 476141795140
Create Date: 2026-03-23

"""

from sqlalchemy import inspect, text

from alembic import op

revision = "a2de92d25263"
down_revision = "476141795140"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)

    # Create enum type (idempotent)
    conn.execute(
        text(
            "DO $$ BEGIN "
            "CREATE TYPE enforcementreason AS ENUM "
            "('overdue', 'fup', 'prepaid', 'admin', 'customer_hold', 'fraud', 'system'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    )

    # Add customer_hold value if enum already exists (idempotent)
    conn.execute(
        text("ALTER TYPE enforcementreason ADD VALUE IF NOT EXISTS 'customer_hold'")
    )

    if not inspector.has_table("enforcement_locks"):
        # Use raw SQL for the reason column to avoid SQLAlchemy trying to
        # re-create the enum type via the before_create event.
        conn.execute(
            text("""
            CREATE TABLE enforcement_locks (
                id UUID PRIMARY KEY,
                subscription_id UUID NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                subscriber_id UUID NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
                reason enforcementreason NOT NULL,
                source VARCHAR(255) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ,
                resolved_by VARCHAR(255),
                notes TEXT,
                CONSTRAINT ck_enforcement_locks_resolved_metadata
                    CHECK (is_active = true OR (resolved_at IS NOT NULL AND resolved_by IS NOT NULL))
            )
        """)
        )

        op.create_index(
            "ix_enforcement_locks_subscription_active",
            "enforcement_locks",
            ["subscription_id", "is_active"],
        )
        op.create_index(
            "ix_enforcement_locks_subscriber_active",
            "enforcement_locks",
            ["subscriber_id", "is_active"],
        )
        # Partial unique index: one active lock per reason per subscription
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_enforcement_locks_active_reason "
                "ON enforcement_locks (subscription_id, reason) "
                "WHERE is_active = true"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = inspect(conn)
    if inspector.has_table("enforcement_locks"):
        op.drop_table("enforcement_locks")
    conn.execute(text("DROP TYPE IF EXISTS enforcementreason"))
