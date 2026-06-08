"""Add IP-block metadata + Splynx provenance to add_ons.

Revision ID: 122_add_addon_ip_fields
Revises: 120_add_idempotency_keys
Create Date: 2026-06-08

Additive columns so public-IP-block add-ons (imported from Splynx) can record
whether they are public and their prefix size, and so the importer is
idempotent via a unique source key. Nothing existing is touched.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "122_add_addon_ip_fields"
down_revision = "120_add_idempotency_keys"
branch_labels = None
depends_on = None

_TABLE = "add_ons"


def _cols(bind) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _cols(bind)
    if "ip_is_public" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "ip_is_public",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "ip_prefix_length" not in existing:
        op.add_column(
            _TABLE, sa.Column("ip_prefix_length", sa.Integer(), nullable=True)
        )
    if "splynx_source" not in existing:
        op.add_column(
            _TABLE, sa.Column("splynx_source", sa.String(length=40), nullable=True)
        )
        op.create_unique_constraint(
            "uq_add_ons_splynx_source", _TABLE, ["splynx_source"]
        )


def downgrade() -> None:
    op.drop_constraint("uq_add_ons_splynx_source", _TABLE, type_="unique")
    op.drop_column(_TABLE, "splynx_source")
    op.drop_column(_TABLE, "ip_prefix_length")
    op.drop_column(_TABLE, "ip_is_public")
