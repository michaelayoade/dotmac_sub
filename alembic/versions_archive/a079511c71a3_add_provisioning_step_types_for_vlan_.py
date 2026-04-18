"""add provisioning step types for vlan and tr069

Revision ID: a079511c71a3
Revises: cc45f3350603
Create Date: 2026-03-19 15:52:01.109265

"""

from alembic import op

revision = 'a079511c71a3'
down_revision = 'cc45f3350603'
branch_labels = None
depends_on = None

# New enum values to add to provisioningsteptype
_NEW_VALUES = [
    "create_olt_service_port",
    "ensure_nas_vlan",
    "push_tr069_wan_config",
    "push_tr069_pppoe_credentials",
]


def upgrade() -> None:
    # PostgreSQL enum ADD VALUE cannot run inside a transaction
    for value in _NEW_VALUES:
        op.execute(
            f"ALTER TYPE provisioningsteptype ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values.
    # The values are harmless if left in place.
    pass
