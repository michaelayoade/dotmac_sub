"""Complete service-extension lifecycle and activity evidence.

Revision ID: 417_service_extension_activity_sot
Revises: 416_binary_device_operational_lifecycle
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "417_service_extension_activity_sot"
down_revision = "416_binary_device_operational_lifecycle"
branch_labels = None
depends_on = None

_ENTRY_UNIQUE = "uq_service_extension_entries_extension_subscription"


def _columns(table_name: str) -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _unique_constraints(table_name: str) -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if constraint.get("name")
    }


def _add_column_if_missing(name: str, column: sa.Column) -> None:
    if name not in _columns("service_extensions"):
        op.add_column("service_extensions", column)


def upgrade() -> None:
    _add_column_if_missing(
        "resumed_count",
        sa.Column(
            "resumed_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    _add_column_if_missing(
        "still_suspended_count",
        sa.Column(
            "still_suspended_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    _add_column_if_missing(
        "canceled_by", sa.Column("canceled_by", sa.String(length=64), nullable=True)
    )
    _add_column_if_missing(
        "canceled_at",
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name in (
        "create_idempotency_key_sha256",
        "create_fingerprint_sha256",
        "apply_idempotency_key_sha256",
        "cancel_idempotency_key_sha256",
    ):
        _add_column_if_missing(
            name, sa.Column(name, sa.String(length=64), nullable=True)
        )
    for name in (
        "create_command_id",
        "create_correlation_id",
        "apply_command_id",
        "apply_correlation_id",
        "cancel_command_id",
        "cancel_correlation_id",
    ):
        _add_column_if_missing(name, sa.Column(name, sa.UUID(), nullable=True))

    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT extension_id, subscription_id, COUNT(*) AS row_count
            FROM service_extension_entries
            GROUP BY extension_id, subscription_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
            )
        )
        .first()
    )
    if duplicates is not None:
        raise RuntimeError(
            "Cannot enforce service-extension entry uniqueness while duplicate "
            "(extension_id, subscription_id) evidence exists. Reconcile the "
            "reviewed duplicate cohort through financial.service_extensions."
        )
    if _ENTRY_UNIQUE not in _unique_constraints("service_extension_entries"):
        op.create_unique_constraint(
            _ENTRY_UNIQUE,
            "service_extension_entries",
            ["extension_id", "subscription_id"],
        )


def downgrade() -> None:
    if _ENTRY_UNIQUE in _unique_constraints("service_extension_entries"):
        op.drop_constraint(
            _ENTRY_UNIQUE,
            "service_extension_entries",
            type_="unique",
        )
    for name in (
        "cancel_correlation_id",
        "cancel_command_id",
        "cancel_idempotency_key_sha256",
        "apply_correlation_id",
        "apply_command_id",
        "apply_idempotency_key_sha256",
        "create_correlation_id",
        "create_command_id",
        "create_fingerprint_sha256",
        "create_idempotency_key_sha256",
        "canceled_at",
        "canceled_by",
        "still_suspended_count",
        "resumed_count",
    ):
        if name in _columns("service_extensions"):
            op.drop_column("service_extensions", name)
