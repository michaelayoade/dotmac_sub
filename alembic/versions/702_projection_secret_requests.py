"""Single-use projection secret fetch authorizations (ADR-0076 section 7).

One row per opened fetch: the credential owner opens it when authoring a
projection plan, the Integrator connector redeems it exactly once, and the
row is burned (`redeemed_at`) in the transaction that releases the secret.
The table holds no secret material.

Revision ID: 702_projection_secret_requests
Revises: 701_olt_ssh_host_key_record
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "702_projection_secret_requests"
down_revision: str | None = "701_olt_ssh_host_key_record"
branch_labels = None
depends_on = None

_TABLE = "projection_secret_requests"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_ref", sa.String(length=240), nullable=False),
        sa.Column(
            "credential_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_credentials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("secret_version", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_ref", name="uq_projection_secret_requests_ref"),
    )
    op.create_index(f"ix_{_TABLE}_request_ref", _TABLE, ["request_ref"])
    op.create_index(f"ix_{_TABLE}_credential_id", _TABLE, ["credential_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_credential_id", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_request_ref", table_name=_TABLE)
    op.drop_table(_TABLE)
