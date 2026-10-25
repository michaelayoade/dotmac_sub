"""Record OLT SSH host-key fingerprints (trust on first use, phase 1).

Adds five nullable columns to ``olt_devices``: key type, SHA256 fingerprint
and first/last seen / last mismatch timestamps. Public-key evidence only.
Additive; no backfill; idempotent.

Revision ID: 701_olt_ssh_host_key_record
Revises: 700_ont_service_config_drift_fields
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "701_olt_ssh_host_key_record"
down_revision: str | None = "700_ont_service_config_drift_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "olt_devices"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("olt_ssh_host_key_type", sa.String(40)),
    ("olt_ssh_host_key_sha256", sa.String(80)),
    ("olt_ssh_host_key_first_seen_at", sa.DateTime(timezone=True)),
    ("olt_ssh_host_key_last_seen_at", sa.DateTime(timezone=True)),
    ("olt_ssh_host_key_last_mismatch_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for name, type_ in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    for name, _type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column(_TABLE, name)
