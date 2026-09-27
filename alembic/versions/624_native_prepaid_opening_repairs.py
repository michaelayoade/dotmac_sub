"""Add account-scoped native prepaid opening repair evidence.

Revision ID: 624_native_prepaid_opening_repairs
Revises: 623_custom_fields_center
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "624_native_prepaid_opening_repairs"
down_revision: str | None = "623_custom_fields_center"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PERMISSION_KEY = "billing:prepaid_funding:native_opening_repair"


def _seed_permission() -> None:
    bind = op.get_bind()
    if not {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        now = datetime.now(UTC)
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description=(
                    "Repair one evidence-backed omitted Sub-native prepaid opening"
                ),
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )
    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin", roles.c.is_active.is_(True)
        )
    ).scalar_one_or_none()
    if admin_id is None:
        return
    linked = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if linked is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(), role_id=admin_id, permission_id=permission_id
            )
        )


def _unseed_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        return
    if "role_permissions" in tables:
        role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
        bind.execute(
            role_permissions.delete().where(
                role_permissions.c.permission_id == permission_id
            )
        )
    bind.execute(permissions.delete().where(permissions.c.id == permission_id))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "native_prepaid_opening_repairs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("original_cutover_batch_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_classification", sa.String(length=40), nullable=False),
        sa.Column("account_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("legacy_handoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculated_amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("splynx_transaction_count", sa.Integer(), nullable=False),
        sa.Column("native_event_count", sa.Integer(), nullable=False),
        sa.Column("cutover_evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_identity_fingerprint", sa.String(64), nullable=False),
        sa.Column("native_evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("shadow_evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("finance_approver_system_user_id", sa.UUID(), nullable=False),
        sa.Column("finance_approver_name", sa.String(160), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticket_reference", sa.String(120), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("operator_system_user_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("applied_by", sa.String(160), nullable=False),
        sa.Column("command_id", sa.UUID(), nullable=False),
        sa.Column("correlation_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_native_prepaid_opening_currency",
        ),
        sa.CheckConstraint(
            "source_classification = 'native_after_handoff'",
            name="ck_native_prepaid_opening_source_classification",
        ),
        sa.CheckConstraint(
            "splynx_transaction_count = 0",
            name="ck_native_prepaid_opening_no_splynx_transactions",
        ),
        sa.CheckConstraint(
            "length(cutover_evidence_fingerprint) = 64 AND "
            "length(source_identity_fingerprint) = 64 AND "
            "length(native_evidence_fingerprint) = 64 AND "
            "length(shadow_evidence_fingerprint) = 64 AND "
            "length(preview_fingerprint) = 64 AND "
            "length(evidence_sha256) = 64 AND evidence_sha256 = lower(evidence_sha256)",
            name="ck_native_prepaid_opening_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["original_cutover_batch_id"],
            ["prepaid_funding_reconstruction_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finance_approver_system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_system_user_id"],
            ["system_users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "currency", name="uq_native_prepaid_opening_account_currency"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_native_prepaid_opening_idempotency"
        ),
    )
    op.create_index(
        "ix_native_prepaid_opening_cutover_batch",
        "native_prepaid_opening_repairs",
        ["original_cutover_batch_id"],
    )
    op.alter_column(
        "customer_subledger_opening_positions",
        "verification_run_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
    op.add_column(
        "customer_subledger_opening_positions",
        sa.Column("native_repair_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_customer_subledger_opening_native_repair",
        "customer_subledger_opening_positions",
        "native_prepaid_opening_repairs",
        ["native_repair_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_customer_subledger_opening_native_repair",
        "customer_subledger_opening_positions",
        ["native_repair_id"],
    )
    op.create_check_constraint(
        "ck_customer_subledger_opening_one_provenance",
        "customer_subledger_opening_positions",
        "(verification_run_id IS NOT NULL AND native_repair_id IS NULL) OR "
        "(verification_run_id IS NULL AND native_repair_id IS NOT NULL)",
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION native_prepaid_opening_repair_append_only()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Native prepaid opening repairs are append-only';
            END;
            $$;
            CREATE TRIGGER native_prepaid_opening_repairs_append_only
            BEFORE UPDATE OR DELETE ON native_prepaid_opening_repairs
            FOR EACH ROW EXECUTE FUNCTION native_prepaid_opening_repair_append_only();
            """
        )
    _seed_permission()


def downgrade() -> None:
    _unseed_permission()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS native_prepaid_opening_repairs_append_only
                ON native_prepaid_opening_repairs;
            DROP FUNCTION IF EXISTS native_prepaid_opening_repair_append_only();
            """
        )
    op.drop_constraint(
        "ck_customer_subledger_opening_one_provenance",
        "customer_subledger_opening_positions",
        type_="check",
    )
    op.drop_constraint(
        "uq_customer_subledger_opening_native_repair",
        "customer_subledger_opening_positions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_customer_subledger_opening_native_repair",
        "customer_subledger_opening_positions",
        type_="foreignkey",
    )
    op.drop_column("customer_subledger_opening_positions", "native_repair_id")
    op.alter_column(
        "customer_subledger_opening_positions",
        "verification_run_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
    op.drop_index(
        "ix_native_prepaid_opening_cutover_batch",
        table_name="native_prepaid_opening_repairs",
    )
    op.drop_table("native_prepaid_opening_repairs")
