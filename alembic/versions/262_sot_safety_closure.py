"""Close SOT credential and capability storage gaps.

Revision ID: 262_sot_safety_closure
Revises: 261_system_user_role_source
Create Date: 2026-07-12
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa

from alembic import op

revision = "262_sot_safety_closure"
down_revision = "261_system_user_role_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "connector_configs",
        "headers",
        existing_type=sa.JSON(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="headers::text",
    )

    op.drop_index("ix_ticket_access_tokens_token", table_name="ticket_access_tokens")
    op.alter_column(
        "ticket_access_tokens",
        "token",
        new_column_name="token_hash",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, token_hash FROM ticket_access_tokens")
    ).mappings()
    for row in rows:
        digest = hashlib.sha256(str(row["token_hash"]).encode("utf-8")).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE ticket_access_tokens SET token_hash = :digest WHERE id = :id"
            ),
            {"digest": digest, "id": row["id"]},
        )
    op.create_index(
        "ix_ticket_access_tokens_token_hash",
        "ticket_access_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ticket_access_tokens_token_hash", table_name="ticket_access_tokens"
    )
    op.alter_column(
        "ticket_access_tokens",
        "token_hash",
        new_column_name="token",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_index(
        "ix_ticket_access_tokens_token",
        "ticket_access_tokens",
        ["token"],
        unique=True,
    )
    op.alter_column(
        "connector_configs",
        "headers",
        existing_type=sa.Text(),
        type_=sa.JSON(),
        existing_nullable=True,
        postgresql_using="to_jsonb(headers)",
    )
