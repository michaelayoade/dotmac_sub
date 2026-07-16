"""Add durable financial access consequence evidence.

Revision ID: 299_financial_access_consequence_evidence
Revises: 298_invoice_closure_evidence
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "299_financial_access_consequence_evidence"
down_revision = "298_invoice_closure_evidence"
branch_labels = None
depends_on = None

_access_action = postgresql.ENUM(
    "suspend",
    "reject",
    "throttle",
    "restore",
    name="financialaccessaction",
    create_type=False,
)
_access_origin = postgresql.ENUM(
    "dunning",
    "prepaid_enforcement",
    "financial_reconciliation",
    "historical_reconciliation",
    name="financialaccessorigin",
    create_type=False,
)
_evidence_operation = postgresql.ENUM(
    "lock_created",
    "lock_resolved",
    "credential_throttled",
    "credential_restored",
    "dunning_case_resolved",
    name="financialaccessevidenceoperation",
    create_type=False,
)
_enforcement_reason = postgresql.ENUM(
    "overdue",
    "fup",
    "prepaid",
    "admin",
    "customer_hold",
    "fraud",
    "system",
    name="enforcementreason",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _access_action.create(bind, checkfirst=True)
    _access_origin.create(bind, checkfirst=True)
    _evidence_operation.create(bind, checkfirst=True)

    op.create_table(
        "financial_access_consequences",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dunning_case_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", _access_action, nullable=False),
        sa.Column("requested_reason", _enforcement_reason, nullable=True),
        sa.Column("origin", _access_origin, nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(120), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("decision_inputs", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dunning_case_id"], ["dunning_cases.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_financial_access_consequence_idempotency",
        ),
    )
    op.add_column(
        "dunning_action_logs",
        sa.Column(
            "access_consequence_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_dunning_action_logs_access_consequence",
        "dunning_action_logs",
        "financial_access_consequences",
        ["access_consequence_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_dunning_action_logs_access_consequence",
        "dunning_action_logs",
        ["access_consequence_id"],
        unique=True,
    )

    op.create_table(
        "financial_access_consequence_evidence",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True
        ),
        sa.Column("consequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enforcement_lock_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("access_credential_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dunning_case_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", _evidence_operation, nullable=False),
        sa.Column("profile_before_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("profile_after_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN enforcement_lock_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN access_credential_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN dunning_case_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_financial_access_evidence_exactly_one_target",
        ),
        sa.ForeignKeyConstraint(
            ["consequence_id"],
            ["financial_access_consequences.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enforcement_lock_id"], ["enforcement_locks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["access_credential_id"], ["access_credentials.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["dunning_case_id"], ["dunning_cases.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_before_id"], ["radius_profiles.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_after_id"], ["radius_profiles.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "consequence_id",
            "enforcement_lock_id",
            "operation",
            name="uq_financial_access_evidence_lock_operation",
        ),
        sa.UniqueConstraint(
            "consequence_id",
            "access_credential_id",
            "operation",
            name="uq_financial_access_evidence_credential_operation",
        ),
        sa.UniqueConstraint(
            "consequence_id",
            "dunning_case_id",
            "operation",
            name="uq_financial_access_evidence_case_operation",
        ),
    )


def downgrade() -> None:
    op.drop_table("financial_access_consequence_evidence")
    op.drop_index(
        "uq_dunning_action_logs_access_consequence",
        table_name="dunning_action_logs",
    )
    op.drop_constraint(
        "fk_dunning_action_logs_access_consequence",
        "dunning_action_logs",
        type_="foreignkey",
    )
    op.drop_column("dunning_action_logs", "access_consequence_id")
    op.drop_table("financial_access_consequences")
    bind = op.get_bind()
    _evidence_operation.drop(bind, checkfirst=True)
    _access_origin.drop(bind, checkfirst=True)
    _access_action.drop(bind, checkfirst=True)
