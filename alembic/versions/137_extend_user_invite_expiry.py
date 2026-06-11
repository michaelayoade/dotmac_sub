"""Extend user invite expiry to 24 hours.

Revision ID: 137_extend_user_invite_expiry
Revises: 136_add_role_grant_scopes
Create Date: 2026-06-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "137_extend_user_invite_expiry"
down_revision = "136_add_role_grant_scopes"
branch_labels = None
depends_on = None

_TABLE = "domain_settings"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET value_text = '1440'
            WHERE domain = 'auth'
              AND key = 'user_invite_expiry_minutes'
              AND value_text = '60'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    op.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET value_text = '60'
            WHERE domain = 'auth'
              AND key = 'user_invite_expiry_minutes'
              AND value_text = '1440'
            """
        )
    )
