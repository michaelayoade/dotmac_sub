"""Close router operation enum drift after the squashed baseline.

The application model and squashed schema contain these router operation
types, but databases upgraded from the pre-squash migration chain can lack
them. UISP CPE writes use ``router_config_push``, so operation creation must
not fail before work is queued.

Revision ID: 270_network_operation_router_types
Revises: 269_ledger_reversal_link
"""

from __future__ import annotations

from alembic import op

revision = "270_network_operation_router_types"
down_revision = "269_ledger_reversal_link"
branch_labels = None
depends_on = None


_ROUTER_OPERATION_TYPES = (
    "router_config_push",
    "router_config_backup",
    "router_reboot",
    "router_firmware_upgrade",
    "router_bulk_push",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for operation_type in _ROUTER_OPERATION_TYPES:
                op.execute(
                    "ALTER TYPE networkoperationtype "
                    f"ADD VALUE IF NOT EXISTS '{operation_type}'"
                )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum values in place. Keeping the values
    # is harmless and avoids rebuilding a type referenced by operation history.
    pass
