"""Add prepaid cutover readiness evidence and retire duplicate min balance.

Revision ID: 319_prepaid_enforcement_readiness
Revises: 318_consolidated_settlement_reconciliation
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "319_prepaid_enforcement_readiness"
down_revision = "318_consolidated_settlement_reconciliation"
branch_labels = None
depends_on = None


def _retire_duplicate_minimum() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, domain, value_text
            FROM domain_settings
            WHERE key = 'prepaid_default_min_balance'
              AND domain IN ('billing', 'collections')
              AND is_active = true
            """
        )
    ).mappings()
    by_domain = {str(row["domain"]): row for row in rows}
    legacy = by_domain.get("collections")
    canonical = by_domain.get("billing")
    if legacy is None:
        return
    if canonical is not None and canonical["value_text"] != legacy["value_text"]:
        raise RuntimeError(
            "Conflicting billing and collections prepaid_default_min_balance "
            "values must be resolved before migration"
        )
    if canonical is None:
        now = datetime.now(UTC)
        connection.execute(
            sa.text(
                """
                INSERT INTO domain_settings (
                    id, domain, key, value_type, value_text, value_json,
                    is_secret, is_active, created_at, updated_at
                ) VALUES (
                    :id, 'billing', 'prepaid_default_min_balance', 'string',
                    :value_text, NULL, false, true, :now, :now
                )
                """
            ),
            {"id": uuid.uuid4(), "value_text": legacy["value_text"], "now": now},
        )
    connection.execute(
        sa.text(
            """
            UPDATE domain_settings
            SET is_active = false, updated_at = :now
            WHERE id = :id
            """
        ),
        {"id": legacy["id"], "now": datetime.now(UTC)},
    )


def upgrade() -> None:
    op.create_table(
        "prepaid_enforcement_readiness",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intended_activation_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("snapshot_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=240), nullable=False),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("candidate_account_count", sa.Integer(), nullable=False),
        sa.Column("candidate_account_ids_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("funding_decisions_hash", sa.String(length=64), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.Column("verified_by", sa.String(length=120), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prepaid_enforcement_readiness_active",
        "prepaid_enforcement_readiness",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_prepaid_enforcement_readiness_activation",
        "prepaid_enforcement_readiness",
        ["intended_activation_at"],
    )
    _retire_duplicate_minimum()


def downgrade() -> None:
    op.drop_index(
        "ix_prepaid_enforcement_readiness_activation",
        table_name="prepaid_enforcement_readiness",
    )
    op.drop_index(
        "uq_prepaid_enforcement_readiness_active",
        table_name="prepaid_enforcement_readiness",
    )
    op.drop_table("prepaid_enforcement_readiness")
