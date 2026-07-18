"""Retire denormalized CRM identifiers from work-order evidence tables.

Every evidence query resolves through the authoritative ``work_order.id``
foreign key. The legacy string columns are redundant and are removed here;
native root rows also return to NULL CRM provenance.

The evidence-consistency gate fails before any column is dropped unless every
stored ``crm_work_order_id`` agrees with the joined ``work_order.public_id``.
A mismatch means the compatibility writer and authoritative foreign key have
diverged and require operator reconciliation.

The work-order root retains ``crm_work_order_id`` only as upstream CRM
provenance. Native rows that received their ``sub-`` public ID in that field
during the compatibility period are normalized back to NULL, preserving the
contract that missing CRM provenance identifies native authority.

Downgrade re-adds the columns nullable. They CANNOT be repopulated — the
values are gone — so downgraded rows carry NULL. This is a one-way retirement
in substance; the downgrade exists only to keep the chain reversible in shape.

Revision ID: 347_work_order_evidence_drop_crm_id
Revises: 346_credit_note_legacy_balance_backfill
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "347_work_order_evidence_drop_crm_id"
down_revision = "346_credit_note_legacy_balance_backfill"
branch_labels = None
depends_on = None

# table -> its FK column back to work_order.id
_EVIDENCE_TABLES: dict[str, str] = {
    "field_worklogs": "work_order_mirror_id",
    "field_work_order_notes": "work_order_mirror_id",
    "field_attachments": "work_order_mirror_id",
    "field_job_chat_messages": "work_order_mirror_id",
    "field_expense_requests": "work_order_mirror_id",
    "field_fiber_test_results": "work_order_mirror_id",
    "field_work_order_materials": "work_order_mirror_id",
    "field_material_requests": "work_order_mirror_id",
    "field_work_order_movements": "work_order_mirror_id",
    "field_job_events": "work_order_mirror_id",
    "work_order_assignment_queue": "work_order_mirror_id",
}


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _has_crm_column(bind, table: str) -> bool:
    """A fresh ``001_squashed`` DB is built from current models, which no
    longer declare ``crm_work_order_id`` — so the column may already be absent
    even though the table exists. Only verify/drop where it is present."""
    return any(
        c["name"] == "crm_work_order_id" for c in sa.inspect(bind).get_columns(table)
    )


def _assert_consistent(bind, present: set[str]) -> None:
    """Refuse to drop if any evidence row's string disagrees with the FK's
    public_id."""
    blockers: dict[str, int] = {}
    for table, fk in _EVIDENCE_TABLES.items():
        if table not in present or not _has_crm_column(bind, table):
            continue
        count = int(
            bind.execute(
                sa.text(
                    f"SELECT count(*) FROM {table} c "  # noqa: S608 - table names are a fixed literal set
                    f"JOIN work_order w ON w.id = c.{fk} "
                    "WHERE c.crm_work_order_id IS DISTINCT FROM w.public_id"
                )
            ).scalar()
            or 0
        )
        if count:
            blockers[table] = count
    if blockers:
        summary = ", ".join(f"{t}={n}" for t, n in blockers.items())
        raise RuntimeError(
            "Evidence rows whose crm_work_order_id disagrees with the joined "
            f"work_order.public_id: {summary}. The dual-write and the FK have "
            "diverged; reconcile the rows before running this migration."
        )


def upgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)

    _assert_consistent(bind, present)

    for table in _EVIDENCE_TABLES:
        if table in present and _has_crm_column(bind, table):
            op.drop_column(table, "crm_work_order_id")

    # Natively-created work orders keep no CRM upstream: retire the
    # transition-era duplicate so the CRM ref is NULL for them. Only native
    # rows carry a "sub-" prefixed id; CRM imports carry UUIDs.
    if "work_order" in present and _has_crm_column(bind, "work_order"):
        bind.execute(
            sa.text(
                "UPDATE work_order SET crm_work_order_id = NULL "
                "WHERE crm_work_order_id LIKE 'sub-%'"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    # Re-add nullable; the retired values are gone and cannot be restored.
    for table in _EVIDENCE_TABLES:
        if table in present and not _has_crm_column(bind, table):
            op.add_column(
                table,
                sa.Column("crm_work_order_id", sa.String(length=64), nullable=True),
            )
