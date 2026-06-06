"""Add payment_webhook_dead_letters for durable inbound-webhook capture.

Revision ID: 117_add_payment_webhook_dead_letters
Revises: 116_add_billing_accounts
Create Date: 2026-06-06

Inbound Paystack/Flutterwave webhooks are captured here before processing so a
transient ingest failure (which now returns HTTP 5xx to trigger a provider
retry) can never silently drop a payment event. Rows are deleted on successful
ingest; failures are retained for replay.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op
from app.models.billing import PaymentWebhookDeadLetterStatus

revision = "117_add_payment_webhook_dead_letters"
down_revision = "116_add_billing_accounts"
branch_labels = None
depends_on = None

_TABLE = "payment_webhook_dead_letters"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE in inspector.get_table_names():
        return

    status_enum = sa.Enum(
        PaymentWebhookDeadLetterStatus,
        name="paymentwebhookdeadletterstatus",
    )
    # create_type defaults to True for PG; harmless/ignored on SQLite.
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider_type", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=True),
        sa.Column("external_id", sa.String(length=160), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default=PaymentWebhookDeadLetterStatus.received.value,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_payment_webhook_dead_letters_status",
        _TABLE,
        ["status"],
    )
    op.create_index(
        "ix_payment_webhook_dead_letters_provider_idem",
        _TABLE,
        ["provider_type", "idempotency_key"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE in inspector.get_table_names():
        op.drop_index("ix_payment_webhook_dead_letters_provider_idem", table_name=_TABLE)
        op.drop_index("ix_payment_webhook_dead_letters_status", table_name=_TABLE)
        op.drop_table(_TABLE)

    if bind.dialect.name == "postgresql":
        sa.Enum(name="paymentwebhookdeadletterstatus").drop(bind, checkfirst=True)
