"""Prepaid activation funding guard: override evidence and permission.

Giving a legacy account (created before customer-subledger authority
activation) its first prepaid subscription while it has no reviewed baseline
or subledger opening puts it in the prepaid funding quarantine, where every
money-based suspension/restoration skips it. The activation guard now refuses
that by default. This table is the only way past the guard: one explicit,
attributable, revocable staff decision per account, never a funding fact. The
account remains in the quarantine signal until its opening is captured.

The ``billing:prepaid_funding:activation_override`` permission is seeded and
granted only to ``admin`` (same shape as ``597_prepaid_draft_repair_permission``)
so no existing role gains the bypass implicitly.

Purely additive; downgrade drops the table and unseeds the permission.

Revision ID: 652_prepaid_activation_funding_overrides
Revises: 651_prepaid_sweep_cycle_totals
Create Date: 2026-10-08
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "652_prepaid_activation_funding_overrides"
down_revision: str | None = "651_prepaid_sweep_cycle_totals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prepaid_activation_funding_overrides"
_PERMISSION_KEY = "billing:prepaid_funding:activation_override"
_PERMISSION_DESCRIPTION = (
    "Admit prepaid activation for a legacy account whose reviewed funding "
    "opening is still missing (account stays funding-quarantined)"
)


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
    now = datetime.now(UTC)

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_PERMISSION_KEY,
                description=_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )

    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin",
            roles.c.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if admin_id is None:
        return
    existing = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid4(),
                role_id=admin_id,
                permission_id=permission_id,
            )
        )


def _unseed_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "permissions" not in tables:
        return
    permission_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = :key"),
        {"key": _PERMISSION_KEY},
    ).scalar()
    if permission_id is None:
        return
    if "role_permissions" in tables:
        bind.execute(
            sa.text("DELETE FROM role_permissions WHERE permission_id = :id"),
            {"id": permission_id},
        )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE id = :id"),
        {"id": permission_id},
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscribers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("quarantine_reason", sa.String(64), nullable=False),
        sa.Column("remediation_runbook", sa.String(160), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.String(160), nullable=False),
        sa.Column(
            "granted_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(160), nullable=True),
        sa.Column(
            "revoked_by_system_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("system_users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name="ck_prepaid_activation_override_currency",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 10",
            name="ck_prepaid_activation_override_reason",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL "
            "AND revoked_by_system_user_id IS NULL AND revoke_reason IS NULL) "
            "OR (revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND revoked_by_system_user_id IS NOT NULL "
            "AND revoke_reason IS NOT NULL)",
            name="ck_prepaid_activation_override_revocation_complete",
        ),
    )
    op.create_index(
        "uq_prepaid_activation_override_active_account",
        _TABLE,
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_prepaid_activation_override_idempotency",
        _TABLE,
        ["idempotency_key"],
        unique=True,
    )
    _seed_permission()


def downgrade() -> None:
    _unseed_permission()
    op.drop_index("uq_prepaid_activation_override_idempotency", table_name=_TABLE)
    op.drop_index("uq_prepaid_activation_override_active_account", table_name=_TABLE)
    op.drop_table(_TABLE)
