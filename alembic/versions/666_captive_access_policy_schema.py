"""Captive access policy schema (expand step).

Revision ID: 666_captive_access_policy_schema
Revises: 665_backfill_splynx_billing_email_contacts
Create Date: 2026-10-10

## Why

Captive (walled-garden) access was decided by one per-account boolean,
``subscribers.captive_redirect_enabled``. The composable policy owned by
``access.captive_access_policy`` replaces it with typed rules scoped to
``global``, ``plan_family`` (optionally narrowed to offers), ``customer_set``
(a named, audited cohort) or ``account``, each with an ``allow``/``deny``
effect and optional category/reseller conditions. Resolution is per
subscription.

Locks stored only the EFFECTIVE treatment, so a later policy change could
never reach them. ``enforcement_locks.requested_access_mode`` records the
treatment the lock's originator requested, so the policy-change coordinator
can re-evaluate a lock without ever exceeding its request.

## What it does (additive only)

1. ``captive_customer_sets`` and ``captive_customer_set_members`` (membership
   history; one open membership per set and account);
2. ``captive_access_rules`` with CHECK constraints binding scope to target;
3. ``captive_access_policy_changes`` (idempotency and outcome evidence);
4. nullable ``enforcement_locks.requested_access_mode`` using the existing
   ``accessrestrictionmode`` type (never re-created here).

No row is read or written. Backfill and verification are revision 667; the
``subscribers.captive_redirect_enabled`` column is untouched (contract step is
a later, separately approved revision).

## Budgets

New empty tables plus one nullable column without default on
``enforcement_locks`` (metadata-only in PostgreSQL 11+). ``lock_timeout = 5s``
so a busy ``enforcement_locks`` fails the upgrade cleanly instead of queueing
writers; retry is safe. Downgrade drops exactly what upgrade created.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "666_captive_access_policy_schema"
down_revision: str | None = "665_backfill_splynx_billing_email_contacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCESS_MODE = postgresql.ENUM(
    "hard_reject", "captive", name="accessrestrictionmode", create_type=False
)


def _uuid(name: str, **kwargs: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), **kwargs)  # type: ignore[arg-type]


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.create_table(
        "captive_customer_sets",
        _uuid("id", primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("name", name="uq_captive_customer_sets_name"),
        sa.CheckConstraint(
            "length(trim(name)) > 0", name="ck_captive_customer_sets_name"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_captive_customer_sets_reason"
        ),
    )

    op.create_table(
        "captive_customer_set_members",
        _uuid("id", primary_key=True),
        _uuid(
            "customer_set_id",
            nullable=False,
        ),
        _uuid("subscriber_id", nullable=False),
        sa.Column("added_by", sa.String(255), nullable=False),
        sa.Column("added_reason", sa.Text(), nullable=False),
        _ts("added_at"),
        _ts("removed_at", nullable=True),
        sa.Column("removed_by", sa.String(255), nullable=True),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["customer_set_id"],
            ["captive_customer_sets.id"],
            ondelete="RESTRICT",
            name="fk_captive_customer_set_members_set",
        ),
        sa.ForeignKeyConstraint(
            ["subscriber_id"],
            ["subscribers.id"],
            ondelete="CASCADE",
            name="fk_captive_customer_set_members_subscriber",
        ),
        sa.CheckConstraint(
            "(removed_at IS NULL) = (removed_by IS NULL)",
            name="ck_captive_customer_set_members_removal_evidence",
        ),
    )
    op.create_index(
        "uq_captive_customer_set_members_open",
        "captive_customer_set_members",
        ["customer_set_id", "subscriber_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_index(
        "ix_captive_customer_set_members_subscriber_open",
        "captive_customer_set_members",
        ["subscriber_id"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    op.create_table(
        "captive_access_rules",
        _uuid("id", primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("effect", sa.String(8), nullable=False),
        _uuid("subscriber_id", nullable=True),
        _uuid("customer_set_id", nullable=True),
        sa.Column("plan_family", sa.String(40), nullable=True),
        sa.Column("offer_ids", sa.JSON(), nullable=True),
        sa.Column("subscriber_categories", sa.JSON(), nullable=True),
        sa.Column("reseller_condition", sa.String(16), nullable=False),
        sa.Column("reseller_ids", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("disabled_at", nullable=True),
        sa.Column("disabled_by", sa.String(255), nullable=True),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["subscriber_id"],
            ["subscribers.id"],
            ondelete="CASCADE",
            name="fk_captive_access_rules_subscriber",
        ),
        sa.ForeignKeyConstraint(
            ["customer_set_id"],
            ["captive_customer_sets.id"],
            ondelete="RESTRICT",
            name="fk_captive_access_rules_customer_set",
        ),
        sa.CheckConstraint(
            "scope IN ('account', 'customer_set', 'plan_family', 'global')",
            name="ck_captive_access_rules_scope",
        ),
        sa.CheckConstraint(
            "effect IN ('allow', 'deny')", name="ck_captive_access_rules_effect"
        ),
        sa.CheckConstraint(
            "reseller_condition IN ('any', 'house', 'specific')",
            name="ck_captive_access_rules_reseller_condition",
        ),
        sa.CheckConstraint(
            "(scope = 'account' AND subscriber_id IS NOT NULL "
            "AND customer_set_id IS NULL AND plan_family IS NULL) OR "
            "(scope = 'customer_set' AND customer_set_id IS NOT NULL "
            "AND subscriber_id IS NULL AND plan_family IS NULL) OR "
            "(scope = 'plan_family' AND plan_family IS NOT NULL "
            "AND subscriber_id IS NULL AND customer_set_id IS NULL) OR "
            "(scope = 'global' AND subscriber_id IS NULL "
            "AND customer_set_id IS NULL AND plan_family IS NULL)",
            name="ck_captive_access_rules_scope_target",
        ),
        sa.CheckConstraint(
            "scope = 'plan_family' OR offer_ids IS NULL",
            name="ck_captive_access_rules_offer_ids_scope",
        ),
        sa.CheckConstraint(
            "(reseller_condition = 'specific') = (reseller_ids IS NOT NULL)",
            name="ck_captive_access_rules_reseller_ids",
        ),
        sa.CheckConstraint(
            "enabled OR (disabled_at IS NOT NULL AND disabled_by IS NOT NULL)",
            name="ck_captive_access_rules_disable_evidence",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_captive_access_rules_reason"
        ),
    )
    op.create_index(
        "ix_captive_access_rules_enabled_scope",
        "captive_access_rules",
        ["enabled", "scope"],
    )
    op.create_index(
        "ix_captive_access_rules_subscriber", "captive_access_rules", ["subscriber_id"]
    )
    op.create_index(
        "ix_captive_access_rules_customer_set",
        "captive_access_rules",
        ["customer_set_id"],
    )

    op.create_table(
        "captive_access_policy_changes",
        _uuid("id", primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        _uuid("command_id", nullable=False),
        sa.Column("change_kind", sa.String(40), nullable=False),
        sa.Column("change_fingerprint", sa.String(64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("max_subscriptions", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.JSON(), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_captive_access_policy_changes_idempotency"
        ),
        sa.CheckConstraint(
            "change_kind IN ('reevaluate', 'add_rule', 'disable_rule', "
            "'create_customer_set', 'add_customer_set_members', "
            "'remove_customer_set_members')",
            name="ck_captive_access_policy_changes_kind",
        ),
    )

    op.add_column(
        "enforcement_locks",
        sa.Column("requested_access_mode", _ACCESS_MODE, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enforcement_locks", "requested_access_mode")
    op.drop_table("captive_access_policy_changes")
    op.drop_index(
        "ix_captive_access_rules_customer_set", table_name="captive_access_rules"
    )
    op.drop_index(
        "ix_captive_access_rules_subscriber", table_name="captive_access_rules"
    )
    op.drop_index(
        "ix_captive_access_rules_enabled_scope", table_name="captive_access_rules"
    )
    op.drop_table("captive_access_rules")
    op.drop_index(
        "ix_captive_customer_set_members_subscriber_open",
        table_name="captive_customer_set_members",
    )
    op.drop_index(
        "uq_captive_customer_set_members_open",
        table_name="captive_customer_set_members",
    )
    op.drop_table("captive_customer_set_members")
    op.drop_table("captive_customer_sets")
