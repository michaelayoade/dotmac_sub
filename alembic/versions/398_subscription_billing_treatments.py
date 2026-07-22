"""Add authoritative subscription billing treatments and non-cash grants.

Revision ID: 398_subscription_billing_treatments
Revises: 397_validate_payment_prepaid_archive
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "398_subscription_billing_treatments"
down_revision = "397_validate_payment_prepaid_archive"
branch_labels = None
depends_on = None

TREATMENTS = ("standard", "complimentary", "sponsored")
REASONS = (
    "internal_service",
    "staff_benefit",
    "partner_service",
    "community_support",
    "commercial_concession",
    "sponsored_service",
    "other_approved",
)
STATUSES = ("active", "revoked")
BILLING_CYCLES = ("daily", "weekly", "monthly", "quarterly", "annual")
PERMISSIONS = {
    "billing:treatment:read": "View subscription billing treatments",
    "billing:treatment:write": "Approve and revoke subscription billing treatments",
}


def _enum(values: tuple[str, ...], name: str) -> sa.types.TypeEngine:
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def _create_postgresql_enums() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    postgresql.ENUM(*TREATMENTS, name="subscription_billing_treatment").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*REASONS, name="billing_treatment_reason").create(
        bind, checkfirst=True
    )
    postgresql.ENUM(*STATUSES, name="billing_treatment_status").create(
        bind, checkfirst=True
    )


def _seed_permissions() -> None:
    bind = op.get_bind()
    if "permissions" not in sa.inspect(bind).get_table_names():
        return
    now = datetime.now(UTC)
    for key, description in PERMISSIONS.items():
        existing = bind.execute(
            sa.text("SELECT id FROM permissions WHERE key = :key"), {"key": key}
        ).scalar()
        if existing:
            bind.execute(
                sa.text(
                    """UPDATE permissions
                    SET description=:description, is_active=true,
                        is_ui_assignable=true, updated_at=:now
                    WHERE key=:key"""
                ),
                {"key": key, "description": description, "now": now},
            )
        else:
            bind.execute(
                sa.text(
                    """INSERT INTO permissions
                    (id,key,description,is_active,is_ui_assignable,created_at,updated_at)
                    VALUES (:id,:key,:description,true,true,:now,:now)"""
                ),
                {
                    "id": str(uuid4()),
                    "key": key,
                    "description": description,
                    "now": now,
                },
            )


def upgrade() -> None:
    _create_postgresql_enums()
    op.create_table(
        "subscription_billing_arrangements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authorized_offer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "treatment",
            _enum(TREATMENTS, "subscription_billing_treatment"),
            nullable=False,
        ),
        sa.Column(
            "reason_code", _enum(REASONS, "billing_treatment_reason"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_policy_max_days", sa.Integer(), nullable=False),
        sa.Column("maximum_recurring_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "billing_cycle", _enum(BILLING_CYCLES, "billingcycle"), nullable=False
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("sponsor_reference", sa.String(200)),
        sa.Column("cost_center", sa.String(100)),
        sa.Column(
            "status", _enum(STATUSES, "billing_treatment_status"), nullable=False
        ),
        sa.Column("approved_by", sa.String(120), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.String(120)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.Text()),
        sa.Column("revocation_command_id", postgresql.UUID(as_uuid=True)),
        sa.Column("revocation_correlation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("revocation_idempotency_key_sha256", sa.String(64)),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(64), nullable=False),
        sa.Column("command_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "treatment IN ('complimentary', 'sponsored')",
            name="ck_subscription_billing_arrangement_nonstandard",
        ),
        sa.CheckConstraint(
            "ends_at > starts_at",
            name="ck_subscription_billing_arrangement_period",
        ),
        sa.CheckConstraint(
            "maximum_recurring_amount > 0",
            name="ck_subscription_billing_arrangement_positive_value",
        ),
        sa.CheckConstraint(
            "approval_policy_max_days BETWEEN 1 AND 366",
            name="ck_subscription_billing_arrangement_approval_policy",
        ),
        sa.CheckConstraint(
            "treatment <> 'sponsored' OR sponsor_reference IS NOT NULL OR cost_center IS NOT NULL",
            name="ck_subscription_billing_arrangement_sponsor_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["authorized_offer_id"], ["catalog_offers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("command_id"),
        sa.UniqueConstraint("revocation_command_id"),
        sa.UniqueConstraint("revocation_idempotency_key_sha256"),
        sa.UniqueConstraint(
            "subscription_id",
            "starts_at",
            name="uq_subscription_billing_arrangement_start",
        ),
    )
    op.create_index(
        "ix_subscription_billing_arrangements_effective",
        "subscription_billing_arrangements",
        ["subscription_id", "status", "starts_at", "ends_at"],
    )
    op.create_index(
        "uq_subscription_billing_arrangements_idempotency",
        "subscription_billing_arrangements",
        ["idempotency_key_sha256"],
        unique=True,
    )

    op.create_table(
        "subscription_billing_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("arrangement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "treatment",
            _enum(TREATMENTS, "subscription_billing_treatment"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("idempotency_key_sha256", sa.String(64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "treatment IN ('complimentary', 'sponsored')",
            name="ck_subscription_billing_grant_nonstandard",
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_subscription_billing_grant_period"
        ),
        sa.CheckConstraint(
            "reference_amount > 0", name="ck_subscription_billing_grant_positive_value"
        ),
        sa.ForeignKeyConstraint(
            ["arrangement_id"],
            ["subscription_billing_arrangements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "arrangement_id",
            "starts_at",
            "ends_at",
            name="uq_subscription_billing_grant_period",
        ),
    )
    op.create_index(
        "uq_subscription_billing_grants_idempotency",
        "subscription_billing_grants",
        ["idempotency_key_sha256"],
        unique=True,
    )
    op.create_index(
        "ix_subscription_billing_grants_subscription_period",
        "subscription_billing_grants",
        ["subscription_id", "starts_at", "ends_at"],
    )
    op.add_column(
        "service_entitlements",
        sa.Column("source_billing_grant_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_service_entitlements_source_billing_grant",
        "service_entitlements",
        "subscription_billing_grants",
        ["source_billing_grant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_service_entitlements_active_billing_grant",
        "service_entitlements",
        ["source_billing_grant_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND source_billing_grant_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "status = 'active' AND source_billing_grant_id IS NOT NULL"
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """CREATE OR REPLACE FUNCTION subscription_billing_grants_append_only()
            RETURNS trigger AS $$ BEGIN
              RAISE EXCEPTION 'subscription billing grants are append-only';
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER subscription_billing_grants_append_only
            BEFORE UPDATE OR DELETE ON subscription_billing_grants
            FOR EACH ROW EXECUTE FUNCTION subscription_billing_grants_append_only();"""
        )
        op.execute(
            """CREATE OR REPLACE FUNCTION protect_subscription_billing_treatment_terms()
            RETURNS trigger AS $$ BEGIN
              IF (
                NEW.offer_id IS DISTINCT FROM OLD.offer_id OR
                NEW.offer_version_id IS DISTINCT FROM OLD.offer_version_id OR
                NEW.billing_mode IS DISTINCT FROM OLD.billing_mode OR
                NEW.billing_cycle IS DISTINCT FROM OLD.billing_cycle OR
                NEW.unit_price IS DISTINCT FROM OLD.unit_price OR
                NEW.discount IS DISTINCT FROM OLD.discount OR
                NEW.discount_value IS DISTINCT FROM OLD.discount_value OR
                NEW.discount_type IS DISTINCT FROM OLD.discount_type OR
                NEW.discount_start_at IS DISTINCT FROM OLD.discount_start_at OR
                NEW.discount_end_at IS DISTINCT FROM OLD.discount_end_at
              ) AND EXISTS (
                SELECT 1
                FROM subscription_billing_arrangements arrangement
                WHERE arrangement.subscription_id = OLD.id
                  AND arrangement.status = 'active'
                  AND arrangement.ends_at > CURRENT_TIMESTAMP
              ) THEN
                RAISE EXCEPTION
                  'revoke the open subscription billing treatment before changing commercial terms'
                  USING ERRCODE = 'check_violation';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER protect_subscription_billing_treatment_terms
            BEFORE UPDATE OF offer_id, offer_version_id, billing_mode, billing_cycle,
              unit_price, discount, discount_value, discount_type, discount_start_at,
              discount_end_at ON subscriptions
            FOR EACH ROW EXECUTE FUNCTION protect_subscription_billing_treatment_terms();"""
        )
    _seed_permissions()


def downgrade() -> None:
    bind = op.get_bind()
    if "permissions" in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text("DELETE FROM permissions WHERE key IN :keys").bindparams(
                sa.bindparam("keys", expanding=True)
            ),
            {"keys": tuple(PERMISSIONS)},
        )
    if bind.dialect.name == "postgresql":
        op.execute("""DROP TRIGGER IF EXISTS protect_subscription_billing_treatment_terms ON subscriptions;
        DROP FUNCTION IF EXISTS protect_subscription_billing_treatment_terms();
        DROP TRIGGER IF EXISTS subscription_billing_grants_append_only ON subscription_billing_grants;
        DROP FUNCTION IF EXISTS subscription_billing_grants_append_only();""")
    op.drop_index(
        "uq_service_entitlements_active_billing_grant",
        table_name="service_entitlements",
    )
    op.drop_constraint(
        "fk_service_entitlements_source_billing_grant",
        "service_entitlements",
        type_="foreignkey",
    )
    op.drop_column("service_entitlements", "source_billing_grant_id")
    op.drop_index(
        "ix_subscription_billing_grants_subscription_period",
        table_name="subscription_billing_grants",
    )
    op.drop_index(
        "uq_subscription_billing_grants_idempotency",
        table_name="subscription_billing_grants",
    )
    op.drop_table("subscription_billing_grants")
    op.drop_index(
        "uq_subscription_billing_arrangements_idempotency",
        table_name="subscription_billing_arrangements",
    )
    op.drop_index(
        "ix_subscription_billing_arrangements_effective",
        table_name="subscription_billing_arrangements",
    )
    op.drop_table("subscription_billing_arrangements")
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name="billing_treatment_status").drop(bind, checkfirst=True)
        postgresql.ENUM(name="billing_treatment_reason").drop(bind, checkfirst=True)
        postgresql.ENUM(name="subscription_billing_treatment").drop(
            bind, checkfirst=True
        )
