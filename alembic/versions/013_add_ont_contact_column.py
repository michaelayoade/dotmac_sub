"""add ont contact column

Revision ID: 013_add_ont_contact_column
Revises: 012_harden_external_identity_constraints
Create Date: 2026-04-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect, text

from alembic import op

revision = "013_add_ont_contact_column"
down_revision = "012_harden_external_identity_constraints"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def upgrade() -> None:
    if not _has_column("ont_units", "contact"):
        op.add_column("ont_units", sa.Column("contact", sa.String(length=255), nullable=True))

    op.execute(
        text(
            """
            UPDATE ont_units
            SET
                contact = NULLIF(
                    BTRIM(
                        CASE
                            WHEN address_or_comment LIKE '---\nLocation Contact: %'
                                THEN SUBSTRING(address_or_comment FROM CHAR_LENGTH('---\nLocation Contact: ') + 1)
                            WHEN address_or_comment LIKE '%\n\n---\nLocation Contact: %'
                                THEN SPLIT_PART(address_or_comment, '\n\n---\nLocation Contact: ', 2)
                            ELSE NULL
                        END
                    ),
                    ''
                ),
                address_or_comment = NULLIF(
                    BTRIM(
                        CASE
                            WHEN address_or_comment LIKE '---\nLocation Contact: %'
                                THEN ''
                            WHEN address_or_comment LIKE '%\n\n---\nLocation Contact: %'
                                THEN SPLIT_PART(address_or_comment, '\n\n---\nLocation Contact: ', 1)
                            ELSE address_or_comment
                        END
                    ),
                    ''
                )
            WHERE
                contact IS NULL
                AND address_or_comment IS NOT NULL
                AND (
                    address_or_comment LIKE '---\nLocation Contact: %'
                    OR address_or_comment LIKE '%\n\n---\nLocation Contact: %'
                )
            """
        )
    )


def downgrade() -> None:
    if _has_column("ont_units", "contact"):
        op.drop_column("ont_units", "contact")
