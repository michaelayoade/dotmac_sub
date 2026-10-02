"""Align Sub's machine credential table with the installed Kernel a97 model.

Sub owns its public lineage; composing Kernel's lineage would mutate other
Sub-owned tables. This transcribes Kernel revision 0028's machine credential
half after Sub revision 551. Existing rows remain unattributed until their
actual owning application is established. Kernel refuses those rows at auth.

Revision ID: 639_machine_attribution
Revises: 638_payment_email_cutover
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "639_machine_attribution"
down_revision = "638_payment_email_cutover"
branch_labels = None
depends_on = None

_TABLE = "machine_credentials"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("source_application", sa.String(64), nullable=True))
    op.add_column(_TABLE, sa.Column("next_key_hash", sa.String(120), nullable=True))
    op.add_column(
        _TABLE,
        sa.Column("rotation_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE, sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_index(
        "ix_machine_credentials_source_application", _TABLE, ["source_application"]
    )
    op.create_unique_constraint(
        "uq_machine_credentials_tenant_next_key_hash",
        _TABLE,
        ["tenant_id", "next_key_hash"],
    )
    op.create_check_constraint(
        "ck_machine_credentials_next_key_hash_scheme",
        _TABLE,
        "next_key_hash IS NULL OR next_key_hash LIKE 'hmac-sha256:%'",
    )
    op.create_check_constraint(
        "ck_machine_credentials_next_key_hash_differs",
        _TABLE,
        "next_key_hash IS NULL OR next_key_hash <> key_hash",
    )
    op.create_check_constraint(
        "ck_machine_credentials_rotation_pair",
        _TABLE,
        "(next_key_hash IS NULL) = (rotation_started_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_machine_credentials_source_application_shape",
        _TABLE,
        "source_application IS NULL OR ("
        "length(source_application) > 1 "
        "AND trim(source_application) = source_application)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_machine_credentials_source_application_shape", _TABLE, type_="check"
    )
    op.drop_constraint("ck_machine_credentials_rotation_pair", _TABLE, type_="check")
    op.drop_constraint(
        "ck_machine_credentials_next_key_hash_differs", _TABLE, type_="check"
    )
    op.drop_constraint(
        "ck_machine_credentials_next_key_hash_scheme", _TABLE, type_="check"
    )
    op.drop_constraint(
        "uq_machine_credentials_tenant_next_key_hash", _TABLE, type_="unique"
    )
    op.drop_index("ix_machine_credentials_source_application", table_name=_TABLE)
    op.drop_column(_TABLE, "rotated_at")
    op.drop_column(_TABLE, "rotation_started_at")
    op.drop_column(_TABLE, "next_key_hash")
    op.drop_column(_TABLE, "source_application")
