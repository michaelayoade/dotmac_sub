"""Rename work_order_mirror to work_order and give it native identity.

WORK_ORDER_IDENTITY_SOT slice 1. The table is Sub's authoritative work-order
storage, not a cache: eleven field-evidence tables hang off it with no
upstream to rebuild from. Identity becomes the Sub-generated ``public_id``
(seeded from ``crm_work_order_id``, which every existing row carries);
``crm_work_order_id`` becomes a nullable provenance reference. Child tables
and their denormalized string columns are untouched in this slice.

Two live-schema states exist (env.py idempotency contract): pre-existing DBs
have ``work_order_mirror`` and need the rename+backfill; fresh squash-built
DBs already got the final-state ``work_order`` from 001's create_all, plus an
empty ``work_order_mirror`` from the guarded 190 — there we assert the mirror
is empty and drop it. rename_table/alter_column are not wrapped by env.py, so
this migration guards itself.

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


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables()
    has_mirror = "work_order_mirror" in tables
    has_native = "work_order" in tables

    if has_native:
        # Fresh squash-built DB: 001 already created work_order in its final
        # model shape (public_id identity, nullable crm ref). The guarded 190
        # left behind an empty work_order_mirror, and later guarded migrations
        # (e.g. 232's named fk_ont_assignments_work_order_mirror_id) attached
        # stray named FKs to it — duplicates of the model FKs that 001 already
        # pointed at work_order. Drop those references, then the vestige.
        # Refuse to guess if the mirror somehow holds data.
        if has_mirror:
            count = conn.execute(
                sa.text("SELECT count(*) FROM work_order_mirror")
            ).scalar()
            if count:
                raise RuntimeError(
                    "Both work_order and work_order_mirror exist and the "
                    f"mirror holds {count} rows; refusing to reconcile "
                    "automatically"
                )
            inspector = sa.inspect(conn)
            for table in inspector.get_table_names():
                if table == "work_order_mirror":
                    continue
                for fk in inspector.get_foreign_keys(table):
                    if fk.get("referred_table") == "work_order_mirror" and fk.get(
                        "name"
                    ):
                        op.drop_constraint(fk["name"], table, type_="foreignkey")
            op.drop_table("work_order_mirror")
        return

    # Pre-existing DB: work_order_mirror is the live table. public_id is
    # seeded from crm_work_order_id, so the seed must be a valid identity:
    # present and unique. Fail before altering anything otherwise.
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
    # Only meaningful on the rename path; a fresh squash-built DB never had
    # the mirror shape to return to.
    tables = _tables()
    if "work_order" not in tables:
        return
    op.alter_column("work_order", "crm_work_order_id", nullable=False)
    op.drop_index("ix_work_order_public_id", table_name="work_order")
    op.drop_column("work_order", "public_id")
    op.rename_table("work_order", "work_order_mirror")
