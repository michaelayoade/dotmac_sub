"""Persist normalized payment-provider event provenance.

Revision ID: 395_provider_event_provenance
Revises: 394_retire_payment_prepaid_applications
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "395_provider_event_provenance"
down_revision = "394_retire_payment_prepaid_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    source_enum = postgresql.ENUM(
        "legacy_unknown",
        "administrative_ingest",
        "verified_webhook",
        "gateway_reconciliation",
        name="paymentprovidereventsource",
        create_type=False,
    )
    payment_status_enum = postgresql.ENUM(
        "pending",
        "succeeded",
        "failed",
        "canceled",
        "partially_refunded",
        "refunded",
        "reversed",
        name="paymentstatus",
        create_type=False,
    )
    if bind.dialect.name == "postgresql":
        source_enum.create(bind, checkfirst=True)
    source_type = source_enum if bind.dialect.name == "postgresql" else sa.String(32)
    payment_status_type = (
        payment_status_enum if bind.dialect.name == "postgresql" else sa.String(32)
    )

    op.add_column(
        "payment_provider_events",
        sa.Column(
            "source",
            source_type,
            nullable=False,
            server_default="legacy_unknown",
        ),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column("observation_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column(
            "observed_payment_status",
            payment_status_type,
            nullable=True,
        ),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column(
            "provider_fee",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column("net_amount", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column("provider_reference", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "payment_provider_events",
        sa.Column("error_code", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_provider_events", "error_code")
    op.drop_column("payment_provider_events", "provider_reference")
    op.drop_column("payment_provider_events", "net_amount")
    op.drop_column("payment_provider_events", "provider_fee")
    op.drop_column("payment_provider_events", "observed_payment_status")
    op.drop_column("payment_provider_events", "observation_digest")
    op.drop_column("payment_provider_events", "source")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(
            name="paymentprovidereventsource",
            create_type=False,
        ).drop(bind, checkfirst=True)
