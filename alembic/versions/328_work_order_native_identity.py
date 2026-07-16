"""Rename work_order_mirror to work_order and give it native identity.

WORK_ORDER_IDENTITY_SOT slice 1. The table is Sub's authoritative work-order
storage, not a cache: eleven field-evidence tables hang off it with no
upstream to rebuild from. Identity becomes the Sub-generated ``public_id``
(seeded from ``crm_work_order_id``, which every existing row carries);
``crm_work_order_id`` becomes a nullable provenance reference. Child tables
and their denormalized string columns are untouched in this slice.

Revision ID: 328_work_order_native_identity
Revises: 327_consolidated_return_document_reconstruction
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "328_work_order_native_identity"
down_revision = "327_consolidated_return_document_reconstruction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # public_id is seeded from crm_work_order_id, so the seed must be a valid
    # identity: present and unique. Fail before altering anything otherwise.
    bad = conn.execute(
        sa.text(
            "SELECT count(*) FROM work_order_mirror "
            "WHERE crm_work_order_id IS NULL OR crm_work_order_id = ''"
        )
    ).scalar()
    if bad:
        raise RuntimeError(
            f"work_order_mirror has {bad} rows without a crm_work_order_id; "
            "cannot seed public_id"
        )
    dup = conn.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT crm_work_order_id FROM "
            "work_order_mirror GROUP BY crm_work_order_id "
            "HAVING count(*) > 1) d"
        )
    ).scalar()
    if dup:
        raise RuntimeError(
            f"work_order_mirror has {dup} duplicated crm_work_order_id "
            "values; cannot seed public_id"
        )

    op.rename_table("work_order_mirror", "work_order")

    op.add_column("work_order", sa.Column("public_id", sa.String(64), nullable=True))
    conn.execute(sa.text("UPDATE work_order SET public_id = crm_work_order_id"))
    op.alter_column("work_order", "public_id", nullable=False)
    op.create_index("ix_work_order_public_id", "work_order", ["public_id"], unique=True)

    op.alter_column("work_order", "crm_work_order_id", nullable=True)


def downgrade() -> None:
    op.alter_column("work_order", "crm_work_order_id", nullable=False)
    op.drop_index("ix_work_order_public_id", table_name="work_order")
    op.drop_column("work_order", "public_id")
    op.rename_table("work_order", "work_order_mirror")
