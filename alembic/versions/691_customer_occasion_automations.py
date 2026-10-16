"""Add disabled-by-default birthday and service anniversary automations."""

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from alembic import op

revision = "691_customer_occasion_automations"
down_revision = "690_backdated_invoice_permissions"
branch_labels = None
depends_on = None


def _create_table() -> sa.Table:
    return op.create_table(
        "customer_occasion_automations",
        sa.Column("occasion", sa.String(20), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("send_hour", sa.Integer(), nullable=False),
        sa.Column("send_minute", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "occasion IN ('birthday', 'anniversary')", name="ck_occasion_kind"
        ),
        sa.CheckConstraint("send_hour BETWEEN 0 AND 23", name="ck_occasion_hour"),
        sa.CheckConstraint("send_minute BETWEEN 0 AND 59", name="ck_occasion_minute"),
        sa.CheckConstraint("revision > 0", name="ck_occasion_revision"),
    )


def upgrade() -> None:
    # The historical squashed baseline creates CURRENT ORM metadata. Preserve
    # its empty table on a fresh chain, while a real predecessor creates it here.
    connection = op.get_bind()
    if sa.inspect(connection).has_table("customer_occasion_automations"):
        table = sa.Table(
            "customer_occasion_automations", sa.MetaData(), autoload_with=connection
        )
        expected = {
            "occasion",
            "enabled",
            "subject",
            "body",
            "timezone",
            "send_hour",
            "send_minute",
            "revision",
            "updated_at",
        }
        if set(table.c.keys()) != expected:
            raise RuntimeError(
                "Customer occasion table shape differs from migration 691."
            )
    else:
        table = _create_table()
    now = datetime.now(UTC)
    op.execute(
        insert(table)
        .values(
            [
                {
                    "occasion": "birthday",
                    "enabled": False,
                    "subject": "Happy birthday, {first_name}!",
                    "body": "Happy birthday, {first_name}! Everyone at Dotmac wishes you a wonderful day.",
                    "timezone": "Africa/Lagos",
                    "send_hour": 9,
                    "send_minute": 0,
                    "revision": 1,
                    "updated_at": now,
                },
                {
                    "occasion": "anniversary",
                    "enabled": False,
                    "subject": "Celebrating {years} years with you",
                    "body": "Hello {first_name}, thank you for being with Dotmac for {years} years. We appreciate you!",
                    "timezone": "Africa/Lagos",
                    "send_hour": 9,
                    "send_minute": 0,
                    "revision": 1,
                    "updated_at": now,
                },
            ],
        )
        .on_conflict_do_nothing(index_elements=["occasion"])
    )

    op.execute(
        sa.text("""
        INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at)
        VALUES
          (gen_random_uuid(), 'notification:occasion:read', 'Review birthday and anniversary email automations', true, true, now(), now()),
          (gen_random_uuid(), 'notification:occasion:update', 'Configure birthday and anniversary email automations', true, true, now(), now()),
          (gen_random_uuid(), 'notification:occasion:test', 'Send explicit birthday and anniversary test emails', true, true, now(), now())
        ON CONFLICT (key) DO NOTHING
    """)
    )
    op.execute(
        sa.text("""
        INSERT INTO role_permissions (id, role_id, permission_id)
        SELECT gen_random_uuid(), roles.id, permissions.id FROM roles CROSS JOIN permissions
        WHERE roles.name = 'admin'
          AND permissions.key IN ('notification:occasion:read', 'notification:occasion:update', 'notification:occasion:test')
        ON CONFLICT (role_id, permission_id) DO NOTHING
    """)
    )


def downgrade() -> None:
    raise RuntimeError(
        "Retire customer occasion configuration with a forward migration."
    )
