"""The receivable projection and its durable run evidence (`receivable-shadow-01`).

`BillingReceivableProjection` is a **rebuildable projection of facts other owners
already decided**. It is not a receivable, it is not an invoice, and nothing may
read it as money. The incumbent writers stay authoritative:

* `financial.invoices` owns invoice lifecycle; `_recalculate_invoice_totals`
  remains the only writer of `invoices.status`, `balance_due` and `paid_at`;
* `financial.payments` owns settlement and allocation;
* `collections.lifecycle` remains the only creator of a `CollectionsCase`, and
  nothing in this projection's chain calls it.

## The monotonic guard is structural, not a convention

Two mechanisms, on purpose, because either alone is a convention:

1. the reconciler's upsert carries
   `WHERE excluded.source_observed_at > billing_receivable_projections.source_observed_at`,
   so a stale observation is a converging no-op rather than an error;
2. a `BEFORE UPDATE` trigger installed by the migration refuses any update that
   does not strictly increase `projection_version`, or that moves
   `source_observed_at` backwards.

(1) makes reconciliation idempotent. (2) makes the invariant unrepresentable —
a future writer that forgets the predicate is refused by the database rather
than quietly overwriting a newer fact with an older one. The trigger is
PostgreSQL-only; the fast SQLite unit lane exercises (1) alone and cannot be
reported as evidence for (2).

`projection_version` is allocated from a database sequence, so it is globally
monotonic across concurrent workers and usable as a watermark by an incremental
reader. It is deliberately a **separate** counter from
`BillingContractVersion.source_version` / `BillingObligation.source_version`,
which answer a different question — "which revision of the upstream contract
source produced these terms" — and are part of
`uq_billing_obligation_natural_identity`. That field keeps its single writer in
`billing.contracts`; this projection only *reads* it, into
`contract_source_version`, as provenance.

## A distinct product projection and module contract

`dotmac-collections` owns the pure peer input `ReceivableObservationV1`.
Sub's persisted, rebuildable row is instead named
`BillingReceivableProjection`. An assembly may map the latter into the former;
neither imports or writes the other, and their distinct names remain safe when
both distributions are installed in one composed product.

## Rebuild

Every column that a rebuild needs to reproduce the row is stored on the row:
the sealed cohort identity, the projection policy version, the exact source row
identifiers, and `input_row_fingerprint` over the ordered source tuple. Replay
the reconciler over the same sealed cohort at the same cutoff and every
`input_row_fingerprint` must reproduce byte for byte. `projection_version`,
`projected_at` and `projected_by_run_id` are the projection's own bookkeeping
and are excluded from that fingerprint — they change on every rebuild by
design, and folding them in would make the projection unable to prove it had
reproduced anything.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

#: Name of the PostgreSQL sequence backing `projection_version`. Shared with
#: the migration and the reconciler so the three cannot disagree about it.
PROJECTION_VERSION_SEQUENCE = "billing_receivable_projection_version_seq"


class ReceivableProjectionRunKind(enum.Enum):
    """What a persisted run was doing. Never inferred from its counts."""

    #: First population of a sealed cohort.
    backfill = "backfill"
    #: Incremental convergence pass over a sealed cohort.
    reconcile = "reconcile"
    #: Detect and repair drift between projection and source.
    drift_repair = "drift_repair"
    #: Read-only semantic parity evaluation, recorded as evidence.
    parity_report = "parity_report"


_run_kind_enum = Enum(ReceivableProjectionRunKind, name="receivableprojectionrunkind")


class BillingReceivableProjection(Base):
    """One projected receivable position, keyed by lane and incumbent invoice.

    This product-owned row stays distinct from the Collections module's pure
    `ReceivableObservationV1` input. A breaking change to the observed field
    set gets an explicit schema/policy version rather than silently mutating
    the evidence contract.
    """

    __tablename__ = "billing_receivable_projections"
    __table_args__ = (
        UniqueConstraint("receivable_key", name="uq_billing_receivable_projection_key"),
        # A rebuild must land on the same row, so the natural identity is also
        # spelled structurally rather than only through the composed key.
        UniqueConstraint(
            "invoice_id",
            "lane",
            name="uq_billing_receivable_projection_invoice_lane",
        ),
        CheckConstraint(
            "observed_total_amount >= 0 AND observed_settled_amount >= 0 "
            "AND observed_outstanding_amount >= 0",
            name="ck_billing_receivable_projection_amount_sign",
        ),
        CheckConstraint(
            "projection_version > 0",
            name="ck_billing_receivable_projection_version_positive",
        ),
        CheckConstraint(
            "length(source_fingerprint) = 64 "
            "AND length(input_row_fingerprint) = 64 "
            "AND length(cohort_definition_seal) = 64 "
            "AND length(service_scope_fingerprint) = 64",
            name="ck_billing_receivable_projection_fingerprints",
        ),
        CheckConstraint(
            "observed_period_end IS NULL OR observed_period_start IS NULL "
            "OR observed_period_end > observed_period_start",
            name="ck_billing_receivable_projection_period",
        ),
        Index("ix_billing_receivable_projection_subscription", "subscription_id"),
        Index("ix_billing_receivable_projection_account_lane", "account_id", "lane"),
        # Backs the incremental reader: "everything projected after watermark".
        Index("ix_billing_receivable_projection_version", "projection_version"),
        # Backs drift detection: "rows whose source moved after we looked".
        Index(
            "ix_billing_receivable_projection_source_observed",
            "source_observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Sealed cohort identity ──────────────────────────────────────────────
    receivable_key: Mapped[str] = mapped_column(String(120), nullable=False)
    lane: Mapped[str] = mapped_column(String(30), nullable=False)
    cohort_name: Mapped[str] = mapped_column(String(80), nullable=False)
    cohort_definition_version: Mapped[str] = mapped_column(String(40), nullable=False)
    cohort_definition_seal: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Monotonic projection bookkeeping ────────────────────────────────────
    #: Sequence-allocated and strictly increasing per row. Enforced by a
    #: BEFORE UPDATE trigger, not by the writer's good intentions.
    projection_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The newest instant among the contributing source rows. The staleness
    #: comparison is made against THIS, never against `projected_at`: a
    #: projection that compares its own clock is comparing when it looked, not
    #: when the fact changed.
    source_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    projected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    projection_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    projected_by_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("receivable_projection_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ── Provenance: the exact authoritative inputs ──────────────────────────
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: The ADR 0007 counterparty, when one exists. NULL is a finding recorded
    #: by the `obligations` parity dimension, never a defect to be papered over.
    contract_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_contract_versions.id", ondelete="RESTRICT"),
    )
    #: Read-only carry of `BillingContractVersion.source_version`. This
    #: projection is not a writer of that field; `billing.contracts` is.
    contract_source_version: Mapped[int | None] = mapped_column(Integer)
    obligation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_obligations.id", ondelete="RESTRICT"),
    )
    invoice_line_ids_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    allocation_ids_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Deterministic fingerprint over every observed source value. A rebuild
    #: that reproduces the cohort must reproduce this exactly.
    input_row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Observed receivable facts ───────────────────────────────────────────
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    observed_total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False
    )
    observed_settled_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False
    )
    #: An OBSERVATION of what the incumbent already holds, recorded once so
    #: parity can compare it. It is NOT a third derivation of outstanding for
    #: consumers to switch to: `collections.postpaid_policy` and
    #: `collections.prepaid_policy` each derive their own from the obligation,
    #: and `invoices.balance_due` is written by `financial.invoices`. A reader
    #: that needs a decision reads those owners, not this column.
    observed_outstanding_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False
    )
    observed_invoice_status: Mapped[str] = mapped_column(String(30), nullable=False)
    observed_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    observed_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    # ── Due-date provenance, carried verbatim ───────────────────────────────
    observed_due_date_basis: Mapped[str | None] = mapped_column(String(40))
    observed_due_date_basis_ref: Mapped[str | None] = mapped_column(String(255))
    observed_due_date_policy_version: Mapped[str | None] = mapped_column(String(64))
    #: What the resolved contract version's payment terms would imply. Recorded
    #: beside the observed value precisely because nothing computes
    #: `invoices.due_at` from `payment_terms_days` today — the divergence is
    #: the finding, and it is reported, never repaired here.
    contract_payment_terms_days: Mapped[int | None] = mapped_column(Integer)

    # ── Observed service scope ──────────────────────────────────────────────
    service_scope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    observed_offer_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    observed_service_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    observed_bundle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    observed_billing_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_billing_cycle: Mapped[str | None] = mapped_column(String(30))
    observed_subscription_status: Mapped[str] = mapped_column(
        String(30), nullable=False
    )

    # ── Observed cadence and proration, from the resolved contract version ──
    observed_collection_timing: Mapped[str | None] = mapped_column(String(20))
    observed_rate_basis: Mapped[str | None] = mapped_column(String(40))
    observed_rate_unit: Mapped[str | None] = mapped_column(String(10))
    observed_rate_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    observed_service_interval_unit: Mapped[str | None] = mapped_column(String(10))
    observed_service_interval_count: Mapped[int | None] = mapped_column(Integer)
    observed_invoice_interval_unit: Mapped[str | None] = mapped_column(String(10))
    observed_invoice_interval_count: Mapped[int | None] = mapped_column(Integer)
    observed_cadence_alignment: Mapped[str | None] = mapped_column(String(40))
    observed_anchor_day: Mapped[int | None] = mapped_column(Integer)
    observed_end_of_month_rule: Mapped[str | None] = mapped_column(String(40))
    observed_timezone_name: Mapped[str | None] = mapped_column(String(64))
    observed_proration_policy: Mapped[str | None] = mapped_column(String(40))

    # ── Billing treatment, and whether it is expressible at the current pin ──
    observed_billing_treatment: Mapped[str] = mapped_column(
        String(20), nullable=False, default="standard"
    )
    #: False when the position carries a non-standard treatment that the pinned
    #: Subscriptions contract cannot express. A false here is what makes the
    #: cadence dimension `not_expressible` rather than silently `matched`.
    billing_treatment_expressible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ReceivableProjectionRun(Base):
    """Durable evidence for one reconcile, backfill, repair, or parity pass.

    Shaped to ADR 0007's cutover-evidence standard: schema and policy version,
    cutoff and observation window, exhaustive cohort classification, source and
    result fingerprints, counts and money totals per currency, the blocker
    categories, and the exact code and schema versions.

    A **dry run persists nothing at all** — no row here, no projected row. That
    is what makes "dry run" mean something stronger than "did not commit": the
    dry-run path never enters the owner-command transaction boundary, so there
    is no write to forget to roll back.
    """

    __tablename__ = "receivable_projection_runs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_receivable_projection_run_idempotency"
        ),
        CheckConstraint(
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
        CheckConstraint(
            "covered_count <= cohort_count",
            name="ck_receivable_projection_run_covered_bound",
        ),
        CheckConstraint(
            "length(cohort_definition_seal) = 64 AND length(membership_digest) = 64 "
            "AND length(source_fingerprint) = 64 AND length(result_fingerprint) = 64",
            name="ck_receivable_projection_run_hashes",
        ),
        CheckConstraint(
            "observation_started_at <= observation_ended_at "
            "AND observation_ended_at <= cutoff_at",
            name="ck_receivable_projection_run_window",
        ),
        Index(
            "ix_receivable_projection_run_kind_cutoff",
            "run_kind",
            "cutoff_at",
        ),
        Index("ix_receivable_projection_run_seal", "cohort_definition_seal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_kind: Mapped[ReceivableProjectionRunKind] = mapped_column(
        _run_kind_enum, nullable=False
    )

    # ── Sealed cohort ───────────────────────────────────────────────────────
    cohort_name: Mapped[str] = mapped_column(String(80), nullable=False)
    cohort_definition_version: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Reproducible from code plus the window alone, with no database.
    cohort_definition_seal: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Reproducible only from the database at the same cutoff. Same seal,
    #: different membership digest, means the SOURCE drifted.
    membership_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)

    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observation_ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # ── Exhaustive cohort classification ────────────────────────────────────
    cohort_count: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ambiguous_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unexpected_unlinked_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_by_status_count: Mapped[int] = mapped_column(Integer, nullable=False)
    not_expressible_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── What the pass did to the projection ─────────────────────────────────
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Observations refused because the stored row already carried a newer
    #: source watermark. Idempotent convergence, not an error.
    stale_skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Equal watermark, different fingerprint. Fails closed: nothing is
    #: written, and the position is surfaced for review rather than resolved
    #: by a coin toss.
    ambiguous_watermark_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    #: Projected rows whose source has left the cohort or vanished. REPORTED,
    #: never deleted: a projection that prunes on a window change loses the
    #: ability to be audited against the evidence recorded on it.
    orphaned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Semantic parity ─────────────────────────────────────────────────────
    parity_matched_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    parity_diverged_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    parity_not_expressible_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    parity_by_dimension: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    #: Named, pinned reasons a claim could not be made, with pin coordinates.
    blockers: Mapped[list[object]] = mapped_column(JSON, nullable=False, default=list)

    currency_totals: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    cohort_classification: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The sequence range this pass allocated, so an incremental reader can
    #: replay exactly the rows one run touched.
    projection_version_low: Mapped[int | None] = mapped_column(BigInteger)
    projection_version_high: Mapped[int | None] = mapped_column(BigInteger)

    code_version: Mapped[str] = mapped_column(String(80), nullable=False)
    database_schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    @property
    def unclassified_count(self) -> int:
        """Candidates the classifier failed to place. Must always be zero.

        The classification is exhaustive by construction, so this is a canary
        for a future edit that adds a candidate path without a bucket — the
        failure mode where a cohort quietly stops covering everything it claims.
        """
        return self.cohort_count - (
            self.covered_count
            + self.unresolved_count
            + self.ambiguous_count
            + self.unexpected_unlinked_count
            + self.duplicate_count
            + self.excluded_by_status_count
            + self.not_expressible_count
        )


__all__ = [
    "PROJECTION_VERSION_SEQUENCE",
    "BillingReceivableProjection",
    "ReceivableProjectionRun",
    "ReceivableProjectionRunKind",
]
