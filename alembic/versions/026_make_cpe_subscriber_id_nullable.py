"""Make cpe_devices.subscriber_id nullable with ON DELETE SET NULL.

Part of DCP-8 (OLT/ONT standalone decoupling): CPE devices can be managed as
inventory without a subscriber owner. The subscriber_id column becomes nullable
and its foreign key is recreated with ``ON DELETE SET NULL`` so that subscriber
removal no longer cascades into CPE inventory loss.

Revision ID: 026_cpe_subscriber_nullable
Revises: 025_acs_interval_3600
Create Date: 2026-04-17

"""

import sqlalchemy as sa

from alembic import op

revision = "026_cpe_subscriber_nullable"
down_revision = "025_acs_interval_3600"
branch_labels = None
depends_on = None


_TABLE = "cpe_devices"
_COLUMN = "subscriber_id"
_FK_NAME = "cpe_devices_subscriber_id_fkey"


def _get_column(inspector: sa.Inspector, column_name: str) -> dict | None:
    for col in inspector.get_columns(_TABLE):
        if col["name"] == column_name:
            return col
    return None


def _find_fk(inspector: sa.Inspector) -> dict | None:
    for fk in inspector.get_foreign_keys(_TABLE):
        if fk.get("constrained_columns") == [_COLUMN]:
            return fk
    return None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    column = _get_column(inspector, _COLUMN)
    if column is not None and not column.get("nullable", False):
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        )

    fk = _find_fk(inspector)
    needs_fk_recreate = fk is None or fk.get("options", {}).get("ondelete") != "SET NULL"

    if fk is not None and needs_fk_recreate:
        op.drop_constraint(fk["name"], _TABLE, type_="foreignkey")
        fk = None

    if fk is None:
        op.create_foreign_key(
            _FK_NAME,
            _TABLE,
            "subscribers",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Revert cpe_devices.subscriber_id to NOT NULL without ON DELETE SET NULL.

    Any rows with NULL subscriber_id will block the NOT NULL alter; the
    downgrade does not attempt to backfill them. Operators must reconcile
    orphan CPE rows manually before running the downgrade.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    fk = _find_fk(inspector)
    if fk is not None:
        op.drop_constraint(fk["name"], _TABLE, type_="foreignkey")

    op.create_foreign_key(
        _FK_NAME,
        _TABLE,
        "subscribers",
        [_COLUMN],
        ["id"],
    )

    column = _get_column(inspector, _COLUMN)
    if column is not None and column.get("nullable", True):
        op.alter_column(
            _TABLE,
            _COLUMN,
            existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        )
