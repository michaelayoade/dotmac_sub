"""Receivable projection, its run evidence, and the structural monotonic guard.

Revision ID: 558_receivable_projection
Revises: 557_outbox_relay_prereq
Create Date: 2026-08-25

Adds `billing_receivable_projections` (the `receivable-shadow-01` projection) and
`receivable_projection_runs` (its ADR 0007-shaped durable evidence). No
authority moves: the incumbent invoice, payment, and collections writers are
untouched by this migration.

Three PostgreSQL-only objects carry the guarantees the ORM cannot:

1. `billing_receivable_projection_version_seq` — the version is allocated
   from a sequence so it is monotonic across concurrent workers rather than
   across one process's memory.
2. `billing_receivable_projections_monotonic()` + its BEFORE UPDATE trigger — an
   update that does not strictly advance `projection_version`, or that moves
   `source_observed_at` backwards, is REFUSED. The reconciler also carries a
   staleness predicate, but a predicate is a convention: a future writer that
   forgets it must still be unable to overwrite a newer fact with an older one.
3. A partial index over rows with no resolved ADR 0007 obligation, because
   "which positions have no counterparty yet" is the query the parity report
   runs on every pass.

## No tenant column, deliberately

`billing_receivable_projections` carries no `tenant_id` and no RLS policy. Its
authoritative inputs — `invoices`, `invoice_lines`, `payment_allocations`,
`subscriptions`, `billing_obligations` — carry none either; Sub is a
single-operator data plane whose tenancy is the ADR-0009 operator bridge, not a
row-level column on financial tables. Adding a tenant column here would invent
a value with no authoritative source and produce an RLS policy that is
decorative rather than isolating. `test_receivable_projection_boundary.py`
asserts the absence structurally, so a later editor cannot half-add one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "558_receivable_projection"
down_revision: str | None = "557_outbox_relay_prereq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS = "billing_receivable_projections"
_RUNS = "receivable_projection_runs"
_SEQUENCE = "billing_receivable_projection_version_seq"
_TRIGGER_FN = "billing_receivable_projections_monotonic"

_RUN_KIND_VALUES = ("backfill", "reconcile", "drift_repair", "parity_report")
#: `create_type=False`: the type is created once, explicitly, below. Letting
#: the column emit its own CREATE TYPE gives the enum two creators, which the
#: incremental-upgrade path rejects and the fresh-database path cannot see.
_RUN_KIND = postgresql.ENUM(
    *_RUN_KIND_VALUES,
    name="receivableprojectionrunkind",
    create_type=False,
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _install_monotonic_guard() -> None:
    """Make a stale or non-advancing update unrepresentable, not merely rare."""
    if not _is_postgres():
        return
    op.execute(
        f"""
        CREATE FUNCTION {_TRIGGER_FN}()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.projection_version <= OLD.projection_version THEN
            RAISE EXCEPTION
              'billing_receivable_projections.projection_version must strictly '
              'increase (% -> %) for receivable_key %',
              OLD.projection_version, NEW.projection_version, OLD.receivable_key;
          END IF;
          IF NEW.source_observed_at < OLD.source_observed_at THEN
            RAISE EXCEPTION
              'billing_receivable_projections.source_observed_at must not move '
              'backwards (% -> %) for receivable_key %',
              OLD.source_observed_at, NEW.source_observed_at, OLD.receivable_key;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TRIGGER_FN}
        BEFORE UPDATE ON {_OBSERVATIONS}
        FOR EACH ROW
        EXECUTE FUNCTION {_TRIGGER_FN}()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_RUN_KIND_VALUES, name="receivableprojectionrunkind").create(
        bind, checkfirst=True
    )

    op.create_table(
        _RUNS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_kind", _RUN_KIND, nullable=False),
        sa.Column("cohort_name", sa.String(length=80), nullable=False),
        sa.Column("cohort_definition_version", sa.String(length=40), nullable=False),
        sa.Column("cohort_definition_seal", sa.String(length=64), nullable=False),
        sa.Column("membership_digest", sa.String(length=64), nullable=False),
        sa.Column("projection_policy_version", sa.String(length=40), nullable=False),
        sa.Column("evidence_schema_version", sa.Integer(), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cohort_count", sa.Integer(), nullable=False),
        sa.Column("covered_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("unexpected_unlinked_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("excluded_by_status_count", sa.Integer(), nullable=False),
        sa.Column("not_expressible_count", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stale_skipped_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "ambiguous_watermark_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("orphaned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "parity_matched_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "parity_diverged_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "parity_not_expressible_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("parity_by_dimension", sa.JSON(), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("currency_totals", sa.JSON(), nullable=False),
        sa.Column("cohort_classification", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("projection_version_low", sa.BigInteger(), nullable=True),
        sa.Column("projection_version_high", sa.BigInteger(), nullable=True),
        sa.Column("code_version", sa.String(length=80), nullable=False),
        sa.Column("database_schema_version", sa.String(length=120), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_receivable_projection_run_idempotency"
        ),
        sa.CheckConstraint(
            "cohort_count >= 0 AND covered_count >= 0 AND unresolved_count >= 0 "
            "AND ambiguous_count >= 0 AND unexpected_unlinked_count >= 0 "
            "AND duplicate_count >= 0 AND excluded_by_status_count >= 0 "
            "AND not_expressible_count >= 0 AND inserted_count >= 0 "
            "AND updated_count >= 0 AND unchanged_count >= 0 "
            "AND stale_skipped_count >= 0 AND ambiguous_watermark_count >= 0 "
            "AND orphaned_count >= 0 AND missing_count >= 0 "
            "AND parity_matched_count >= 0 AND parity_diverged_count >= 0 "
            "AND parity_not_expressible_count >= 0",
            name="ck_receivable_projection_run_nonnegative",
        ),
        sa.CheckConstraint(
            "covered_count <= cohort_count",
            name="ck_receivable_projection_run_covered_bound",
        ),
        sa.CheckConstraint(
            "length(cohort_definition_seal) = 64 AND length(membership_digest) = 64 "
            "AND length(source_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_receivable_projection_run_hashes",
        ),
        sa.CheckConstraint(
            "observation_started_at <= observation_ended_at "
            "AND observation_ended_at <= cutoff_at",
            name="ck_receivable_projection_run_window",
        ),
    )
    op.create_index(
        "ix_receivable_projection_run_kind_cutoff",
        _RUNS,
        ["run_kind", "cutoff_at"],
    )
    op.create_index(
        "ix_receivable_projection_run_seal", _RUNS, ["cohort_definition_seal"]
    )

    op.create_table(
        _OBSERVATIONS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receivable_key", sa.String(length=120), nullable=False),
        sa.Column("lane", sa.String(length=30), nullable=False),
        sa.Column("cohort_name", sa.String(length=80), nullable=False),
        sa.Column("cohort_definition_version", sa.String(length=40), nullable=False),
        sa.Column("cohort_definition_seal", sa.String(length=64), nullable=False),
        sa.Column("projection_version", sa.BigInteger(), nullable=False),
        sa.Column("source_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projection_policy_version", sa.String(length=40), nullable=False),
        sa.Column("projected_by_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contract_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contract_source_version", sa.Integer(), nullable=True),
        sa.Column("obligation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invoice_line_ids_sha256", sa.String(length=64), nullable=False),
        sa.Column("allocation_ids_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_row_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("observed_total_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("observed_settled_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("observed_outstanding_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("observed_invoice_status", sa.String(length=30), nullable=False),
        sa.Column("observed_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_due_date_basis", sa.String(length=40), nullable=True),
        sa.Column("observed_due_date_basis_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "observed_due_date_policy_version", sa.String(length=64), nullable=True
        ),
        sa.Column("contract_payment_terms_days", sa.Integer(), nullable=True),
        sa.Column("service_scope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("observed_offer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "observed_offer_version_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "observed_service_address_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("observed_bundle_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observed_billing_mode", sa.String(length=20), nullable=False),
        sa.Column("observed_billing_cycle", sa.String(length=30), nullable=True),
        sa.Column("observed_subscription_status", sa.String(length=30), nullable=False),
        sa.Column("observed_collection_timing", sa.String(length=20), nullable=True),
        sa.Column("observed_rate_basis", sa.String(length=40), nullable=True),
        sa.Column("observed_rate_unit", sa.String(length=10), nullable=True),
        sa.Column("observed_rate_quantity", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "observed_service_interval_unit", sa.String(length=10), nullable=True
        ),
        sa.Column("observed_service_interval_count", sa.Integer(), nullable=True),
        sa.Column(
            "observed_invoice_interval_unit", sa.String(length=10), nullable=True
        ),
        sa.Column("observed_invoice_interval_count", sa.Integer(), nullable=True),
        sa.Column("observed_cadence_alignment", sa.String(length=40), nullable=True),
        sa.Column("observed_anchor_day", sa.Integer(), nullable=True),
        sa.Column("observed_end_of_month_rule", sa.String(length=40), nullable=True),
        sa.Column("observed_timezone_name", sa.String(length=64), nullable=True),
        sa.Column("observed_proration_policy", sa.String(length=40), nullable=True),
        sa.Column(
            "observed_billing_treatment",
            sa.String(length=20),
            nullable=False,
            server_default="standard",
        ),
        sa.Column(
            "billing_treatment_expressible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["projected_by_run_id"], [f"{_RUNS}.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["contract_version_id"],
            ["billing_contract_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["obligation_id"], ["billing_obligations.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "receivable_key", name="uq_billing_receivable_projection_key"
        ),
        sa.UniqueConstraint(
            "invoice_id",
            "lane",
            name="uq_billing_receivable_projection_invoice_lane",
        ),
        sa.CheckConstraint(
            "observed_total_amount >= 0 AND observed_settled_amount >= 0 "
            "AND observed_outstanding_amount >= 0",
            name="ck_billing_receivable_projection_amount_sign",
        ),
        sa.CheckConstraint(
            "projection_version > 0",
            name="ck_billing_receivable_projection_version_positive",
        ),
        sa.CheckConstraint(
            "length(source_fingerprint) = 64 "
            "AND length(input_row_fingerprint) = 64 "
            "AND length(cohort_definition_seal) = 64 "
            "AND length(service_scope_fingerprint) = 64",
            name="ck_billing_receivable_projection_fingerprints",
        ),
        sa.CheckConstraint(
            "observed_period_end IS NULL OR observed_period_start IS NULL "
            "OR observed_period_end > observed_period_start",
            name="ck_billing_receivable_projection_period",
        ),
    )
    op.create_index(
        "ix_billing_receivable_projection_subscription",
        _OBSERVATIONS,
        ["subscription_id"],
    )
    op.create_index(
        "ix_billing_receivable_projection_account_lane",
        _OBSERVATIONS,
        ["account_id", "lane"],
    )
    op.create_index(
        "ix_billing_receivable_projection_version",
        _OBSERVATIONS,
        ["projection_version"],
    )
    op.create_index(
        "ix_billing_receivable_projection_source_observed",
        _OBSERVATIONS,
        ["source_observed_at"],
    )

    if _is_postgres():
        op.execute(f"CREATE SEQUENCE {_SEQUENCE} AS bigint START WITH 1 INCREMENT BY 1")
        # "Which positions still have no ADR 0007 counterparty" is run on every
        # parity pass; a partial index keeps it from degrading into a scan of
        # the whole projection as the covered set grows.
        op.execute(
            f"CREATE INDEX ix_billing_receivable_projection_unlinked_obligation "
            f"ON {_OBSERVATIONS} (subscription_id) WHERE obligation_id IS NULL"
        )
    _install_monotonic_guard()


def downgrade() -> None:
    if _is_postgres():
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TRIGGER_FN} ON {_OBSERVATIONS}")
        op.execute(f"DROP FUNCTION IF EXISTS {_TRIGGER_FN}()")
        op.execute(
            "DROP INDEX IF EXISTS ix_billing_receivable_projection_unlinked_obligation"
        )
    op.drop_index(
        "ix_billing_receivable_projection_source_observed",
        table_name=_OBSERVATIONS,
    )
    op.drop_index("ix_billing_receivable_projection_version", table_name=_OBSERVATIONS)
    op.drop_index(
        "ix_billing_receivable_projection_account_lane", table_name=_OBSERVATIONS
    )
    op.drop_index(
        "ix_billing_receivable_projection_subscription", table_name=_OBSERVATIONS
    )
    op.drop_table(_OBSERVATIONS)
    op.drop_index("ix_receivable_projection_run_seal", table_name=_RUNS)
    op.drop_index("ix_receivable_projection_run_kind_cutoff", table_name=_RUNS)
    op.drop_table(_RUNS)
    if _is_postgres():
        op.execute(f"DROP SEQUENCE IF EXISTS {_SEQUENCE}")
        postgresql.ENUM(name="receivableprojectionrunkind").drop(
            op.get_bind(), checkfirst=True
        )
