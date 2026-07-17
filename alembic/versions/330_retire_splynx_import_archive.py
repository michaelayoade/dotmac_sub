"""Retire the empty Splynx import archive.

Revision ID: 330_retire_splynx_import_archive
Revises: 329_subscriber_field_verifications

Splynx was the pre-migration BSS and has no write path into this system. The
archive models it left behind were never populated: every target table holds
zero rows in production (verified 2026-07-17). Unlike the VAS retirement
(revision 300), there is no financial history to preserve here — keeping these
tables preserves nothing and leaves a schema implying an archive exists.

The emptiness is re-checked against the live database rather than assumed: a
non-empty table blocks the drop with a per-table breakdown. See
docs/designs/SPLYNX_RETIREMENT.md.

``Subscriber.splynx_customer_id`` is deliberately NOT touched. It is populated
on 99.8% of subscribers and is the provenance reference CRM linkage resolves
through; it retires with CRM.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "330_retire_splynx_import_archive"
down_revision = "329_subscriber_field_verifications"
branch_labels = None
depends_on = None

# Child tables first: FKs point child -> parent, so this order is also the
# safe drop order.
_RETIRED_TABLES = (
    "splynx_archived_ticket_messages",
    "splynx_archived_tickets",
    "splynx_archived_quote_items",
    "splynx_archived_quotes",
    "portal_onboarding_states",
)


def _has_table(bind, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _assert_safe_cutover(bind) -> None:
    """Refuse to drop a table that holds rows.

    Production is empty today. This gate exists for every other environment,
    and for the possibility that the fact changed between the decision and the
    deploy. A non-empty table means someone imported data this retirement did
    not account for — re-open the decision rather than truncating.
    """
    blockers: dict[str, int] = {}
    for table_name in _RETIRED_TABLES:
        if not _has_table(bind, table_name):
            continue
        count = int(
            bind.execute(
                sa.text(f"SELECT COUNT(*) FROM {table_name}")  # noqa: S608
            ).scalar()
            or 0
        )
        if count:
            blockers[table_name] = count
    if blockers:
        summary = ", ".join(f"{name}={count}" for name, count in blockers.items())
        raise RuntimeError(
            "Splynx archive retirement blocked: these tables are not empty and "
            f"dropping them would delete data: {summary}. This retirement is "
            "predicated on the archive being empty (see "
            "docs/designs/SPLYNX_RETIREMENT.md) — re-open the decision instead "
            "of truncating."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _assert_safe_cutover(bind)
    for table_name in _RETIRED_TABLES:
        op.drop_table(table_name)


def downgrade() -> None:
    """Recreate the retired tables, empty.

    A faithful rollback: the tables held no rows when dropped, so there is
    nothing to restore. This exists so a code rollback to a release that still
    registers these models finds the schema it expects.
    """
    op.create_table(
        "splynx_archived_tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("splynx_ticket_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("assigned_to", sa.String(length=160), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("splynx_metadata", JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "splynx_archived_ticket_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("splynx_message_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "ticket_id",
            UUID(as_uuid=True),
            sa.ForeignKey("splynx_archived_tickets.id"),
            nullable=False,
        ),
        sa.Column("sender_type", sa.String(length=20), nullable=False),
        sa.Column("sender_name", sa.String(length=160), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("is_internal", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "splynx_archived_quotes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("splynx_quote_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id"),
            nullable=True,
        ),
        sa.Column("quote_number", sa.String(length=60), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=True),
        sa.Column("tax_total", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("splynx_metadata", JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "splynx_archived_quote_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("splynx_item_id", sa.Integer(), nullable=True),
        sa.Column(
            "quote_id",
            UUID(as_uuid=True),
            sa.ForeignKey("splynx_archived_quotes.id"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "portal_onboarding_states",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "subscriber_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("steps_completed", sa.Integer(), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
