"""record exact service-extension grant intervals

Revision ID: 417_service_extension_grant_intervals
Revises: 416_binary_device_operational_lifecycle
Create Date: 2026-07-24

Historical entries retain their original previous/new billing interval. New
code may choose application time as the start for a stale billing anchor, but
this migration does not reinterpret already-applied compensation.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "417_service_extension_grant_intervals"
down_revision = "416_binary_device_operational_lifecycle"
branch_labels = None
depends_on = None

_GRANT_END_INDEX = "ix_service_extension_entries_subscriber_grant_end"
_ENTRY_UNIQUE_INDEX = "uq_service_extension_entries_extension_subscription"


def upgrade() -> None:
    op.add_column(
        "service_extension_entries",
        sa.Column("grant_starts_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "service_extension_entries",
        sa.Column("grant_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "service_extension_entries",
        sa.Column("anchor_basis", sa.String(length=40), nullable=True),
    )

    # Preserve the exact interval historically recorded by each legacy row.
    op.execute(
        sa.text(
            """
            UPDATE service_extension_entries
            SET grant_starts_at = previous_next_billing_at,
                grant_ends_at = new_next_billing_at,
                anchor_basis = 'legacy_previous_anchor'
            WHERE previous_next_billing_at IS NOT NULL
              AND new_next_billing_at IS NOT NULL
              AND new_next_billing_at > previous_next_billing_at
            """
        )
    )
    op.create_index(
        _GRANT_END_INDEX,
        "service_extension_entries",
        ["subscriber_id", "grant_ends_at"],
    )
    duplicate_group_count = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT COUNT(*)
            FROM (
                SELECT extension_id, subscription_id
                FROM service_extension_entries
                GROUP BY extension_id, subscription_id
                HAVING COUNT(*) > 1
            ) AS duplicate_groups
            """
            )
        )
        .scalar_one()
    )
    if duplicate_group_count:
        raise RuntimeError(
            "Cannot enforce service-extension entry identity: "
            f"{duplicate_group_count} duplicate extension/subscription group(s) "
            "require reviewed reconciliation."
        )
    op.create_index(
        _ENTRY_UNIQUE_INDEX,
        "service_extension_entries",
        ["extension_id", "subscription_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(_ENTRY_UNIQUE_INDEX, table_name="service_extension_entries")
    op.drop_index(_GRANT_END_INDEX, table_name="service_extension_entries")
    op.drop_column("service_extension_entries", "anchor_basis")
    op.drop_column("service_extension_entries", "grant_ends_at")
    op.drop_column("service_extension_entries", "grant_starts_at")
