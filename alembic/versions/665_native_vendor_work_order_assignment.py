"""Expand native vendor assignment and explicit vendor field actors.

Revision ID: 665_native_vendor_work_order_assignment
Revises: 664_purge_retired_splynx_metadata_keys
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "665_native_vendor_work_order_assignment"
down_revision = "664_purge_retired_splynx_metadata_keys"
branch_labels = None
depends_on = None

ACTORS = (
    (
        "field_job_events",
        "author",
        "person_id",
        "author_technician_id",
        "system_user_id",
    ),
    ("field_worklogs", "author", "person_id", "author_technician_id", "system_user_id"),
    (
        "field_work_order_notes",
        "author",
        "author_person_id",
        "author_technician_id",
        "author_system_user_id",
    ),
    (
        "field_work_order_movements",
        "actor",
        "actor_person_id",
        "actor_technician_id",
        "actor_system_user_id",
    ),
    (
        "field_attachments",
        "uploaded_by",
        "uploaded_by_person_id",
        "uploaded_by_technician_id",
        "uploaded_by_system_user_id",
    ),
)


def upgrade() -> None:
    # Existing contradictory assignments require owner-led reconciliation. Do
    # not silently select a winner or turn arbitrary metadata into authority.
    bind = op.get_bind()
    invalid = bind.execute(
        sa.text("""
        SELECT 1 FROM work_order_assignment_queue
        WHERE status = 'assigned' AND assigned_technician_id IS NULL
        UNION ALL
        SELECT 1 FROM work_order_assignment_queue WHERE status = 'assigned'
        GROUP BY work_order_mirror_id HAVING count(*) > 1 LIMIT 1
    """)
    ).first()
    if invalid:
        raise RuntimeError(
            "Contradictory legacy work-order assignments; reconcile before migration"
        )
    op.add_column(
        "work_order_assignment_queue",
        sa.Column("assigned_vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_work_order_queue_vendor",
        "work_order_assignment_queue",
        "vendors",
        ["assigned_vendor_id"],
        ["id"],
    )
    op.create_check_constraint(
        "ck_work_order_assignment_target",
        "work_order_assignment_queue",
        "status != 'assigned' OR ((assigned_technician_id IS NOT NULL AND assigned_vendor_id IS NULL) OR (assigned_technician_id IS NULL AND assigned_vendor_id IS NOT NULL))",
    )
    op.create_index(
        "uq_work_order_current_assignment",
        "work_order_assignment_queue",
        ["work_order_mirror_id"],
        unique=True,
        postgresql_where=sa.text("status = 'assigned'"),
    )
    op.create_table(
        "work_order_assignment_receipts",
        sa.Column("command_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(160), unique=True, nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
    )
    for table, prefix, person, technician, system_user in ACTORS:
        vendor = f"{prefix}_vendor_user_id"
        op.add_column(
            table, sa.Column(vendor, postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_{table}_vendor_actor", table, "field_vendor_users", [vendor], ["id"]
        )
        op.alter_column(
            table, person, existing_type=postgresql.UUID(as_uuid=True), nullable=True
        )
        op.alter_column(
            table,
            technician,
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )
        staff = (
            f"{person} IS NOT NULL"
            if table == "field_attachments"
            else f"{technician} IS NOT NULL AND {person} IS NOT NULL"
        )
        op.create_check_constraint(
            f"ck_{table}_actor",
            table,
            f"({vendor} IS NULL AND {staff}) OR ({vendor} IS NOT NULL AND {technician} IS NULL AND {person} IS NULL AND {system_user} IS NOT NULL)",
        )
    op.create_index(
        "uq_field_notes_vendor_client_ref",
        "field_work_order_notes",
        ["author_vendor_user_id", "client_ref"],
        unique=True,
        postgresql_where=sa.text("client_ref IS NOT NULL"),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Vendor assignment/actor expansion is forward-only; vendor evidence must be preserved"
    )
