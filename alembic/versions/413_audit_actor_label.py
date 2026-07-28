"""add actor_label to audit_events

Stores the resolved human label (person name / API-key label) alongside the
raw actor id, so audit can be searched by person without a join and reads
correctly after the referenced actor is deleted. Backfills existing rows from
the actor_name already snapshotted into metadata where present.

Revision ID: 413_audit_actor_label
Revises: 412_payment_gateway_control_plane
Create Date: 2026-07-24

"""

import sqlalchemy as sa

from alembic import op

revision = "413_audit_actor_label"
down_revision = "412_payment_gateway_control_plane"
branch_labels = None
depends_on = None

_INDEX = "ix_audit_events_actor_label"


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {i["name"] for i in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists("audit_events", "actor_label"):
        op.add_column(
            "audit_events",
            sa.Column("actor_label", sa.String(length=160), nullable=True),
        )
    if not _index_exists("audit_events", _INDEX):
        op.create_index(_INDEX, "audit_events", ["actor_label"])

    # Backfill from the label already snapshotted into metadata. Both backends
    # in use expose JSON member access; guard each so a missing key is skipped
    # rather than erroring.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            """
            UPDATE audit_events
            SET actor_label = COALESCE(
                metadata ->> 'actor_name',
                metadata ->> 'actor_email'
            )
            WHERE actor_label IS NULL
              AND metadata IS NOT NULL
            """
        )
    else:  # sqlite (tests)
        bind.exec_driver_sql(
            """
            UPDATE audit_events
            SET actor_label = COALESCE(
                json_extract(metadata, '$.actor_name'),
                json_extract(metadata, '$.actor_email')
            )
            WHERE actor_label IS NULL
              AND metadata IS NOT NULL
            """
        )


def downgrade() -> None:
    if _index_exists("audit_events", _INDEX):
        op.drop_index(_INDEX, table_name="audit_events")
    if _column_exists("audit_events", "actor_label"):
        op.drop_column("audit_events", "actor_label")
