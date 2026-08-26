"""`billing.receivable_projection`: reconciler for `billing_receivable_projections`.

Sole canonical writer of `billing_receivable_projections` and
`receivable_projection_runs`. It reads what the incumbent owners already
decided and writes an observation; it decides nothing about money, access, or
collections, and it never creates a collections case.

## What it does not touch

* `financial.invoices` keeps `_recalculate_invoice_totals` as the only writer of
  `invoices.status`, `balance_due` and `paid_at`.
* `financial.payments` keeps allocation and settlement.
* `financial.payment_provider_events` keeps the settlement mirror — the single
  `PaymentProviderEvent(...)` construction site is untouched by this module,
  which never imports it.
* `collections.lifecycle` keeps the only `CollectionsCase(...)` construction
  site. Nothing in this package imports it, and
  `tests/architecture/test_receivable_projection_boundary.py` enforces that.

## Dry run is the default, structurally

`ProjectionMode.DRY_RUN` is the dataclass default and the CLI has no
`--dry-run` flag — only `--apply` — so omitting an argument can only mean dry
run, and there is no spelling of "dry run" that a typo can turn into a write.

A dry run does not enter `execute_owner_command` at all. It builds the same
plan the apply path builds, returns the same typed result, and persists
nothing: not a projected row, not a run row. That is stronger than "opened a
transaction and rolled it back" — there is no write to forget to undo.

## Idempotency and the monotonic guard

One pass converges: re-running with unchanged sources produces
`unchanged_count == covered_count` and writes nothing. The staleness rule is
enforced three times over, and each layer catches what the one below cannot:

1. *plan* — the pre-loaded projection row's `source_observed_at` is compared to
   the freshly derived watermark, so a stale observation is classified before
   any statement is issued;
2. *statement* — on PostgreSQL the upsert is
   `ON CONFLICT (receivable_key) DO UPDATE ... WHERE excluded.source_observed_at
   > billing_receivable_projections.source_observed_at`, which closes the window
   between the plan's read and the write;
3. *schema* — a BEFORE UPDATE trigger refuses any update that does not strictly
   advance `projection_version` or that moves `source_observed_at` backwards,
   so a future writer that forgets (2) is refused by the database.

Equal watermark with a different fingerprint fails closed: nothing is written
and the position is counted in `ambiguous_watermark_count`. Two facts at the
same instant is not a tie to be broken by whichever query ran last.

## Orphans are reported, never pruned

A projected row whose invoice left the cohort is counted in `orphaned_count`
and left alone. Deleting projection rows when a window moves would destroy the
ability to audit a run against the evidence recorded for it, and "the cohort
changed" is not the same fact as "this observation was wrong".
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from sqlalchemy import Table, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.billing import (
    CreditNoteApplication,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
)
from app.models.billing_contract import (
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingObligation,
)
from app.models.billing_receivable_projection import (
    PROJECTION_VERSION_SEQUENCE,
    BillingReceivableProjection,
    ReceivableProjectionRun,
    ReceivableProjectionRunKind,
)
from app.models.catalog import Subscription
from app.models.subscription_billing_treatment import (
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
)
from app.services.billing.receivable_cohort import (
    COHORT_DEFINITION_VERSION,
    COHORT_NAME,
    DECLARED_INVOICE_STATUSES,
    EVIDENCE_SCHEMA_VERSION,
    EXCLUDED_INVOICE_STATUSES,
    PROJECTION_POLICY_VERSION,
    STANDING_BLOCKERS,
    UNADOPTED_BILLING_TREATMENTS,
    CohortClassification,
    ReceivableCohortWindow,
    ReceivableLane,
    definition_seal,
    digest_payload,
    lane_for_billing_mode,
    membership_digest,
    receivable_key,
)
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = "billing.receivable_projection"

_RECONCILE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="billing receivable projection",
    name="reconcile_receivable_projection",
)

#: Transaction-level advisory lock. Two operator or scheduled passes over the
#: same cohort must not interleave their upserts; the natural key would still
#: arbitrate each row, but the run evidence would describe a mixture of both.
_RECONCILE_LOCK_KEY = 328_160_741

_ZERO = Decimal("0.0000")

#: Written into `result_fingerprint` while a run row is still open. It is a
#: syntactically valid digest so the CHECK constraint accepts the insert, and
#: it is overwritten by `_close_run` before the owner transaction commits.
_PENDING_FINGERPRINT = "0" * 64


class ReceivableProjectionError(DomainError):
    """Fail-closed rejection from the receivable projection owner."""


class ProjectionMode(StrEnum):
    """Whether a pass may write. `DRY_RUN` is every command's default."""

    DRY_RUN = "dry_run"
    APPLY = "apply"


class ApplyOutcome(StrEnum):
    """What one planned observation did to the projection."""

    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    #: The stored row already carried a newer source watermark.
    STALE_SKIPPED = "stale_skipped"
    #: Equal watermark, different fingerprint. Nothing written.
    AMBIGUOUS_WATERMARK = "ambiguous_watermark"


@dataclass(frozen=True, slots=True)
class ObservedPosition:
    """One receivable position, fully derived and ready to project.

    A plain value object: building it touches no session state and writes
    nothing, which is what lets the dry-run and apply paths share exactly one
    derivation instead of two that drift.
    """

    receivable_key: str
    lane: ReceivableLane
    invoice_id: uuid.UUID
    account_id: uuid.UUID
    subscription_id: uuid.UUID
    contract_version_id: uuid.UUID | None
    contract_source_version: int | None
    obligation_id: uuid.UUID | None
    source_observed_at: datetime
    source_fingerprint: str
    input_row_fingerprint: str
    invoice_line_ids_sha256: str
    allocation_ids_sha256: str
    service_scope_fingerprint: str
    values: dict[str, object]

    @property
    def currency(self) -> str:
        return str(self.values["currency"])

    @property
    def outstanding(self) -> Decimal:
        return Decimal(str(self.values["observed_outstanding_amount"]))


@dataclass(frozen=True, slots=True)
class CandidateDisposition:
    """How one candidate invoice was classified, and why."""

    invoice_id: uuid.UUID
    classification: CohortClassification
    detail: str
    position: ObservedPosition | None = None


@dataclass(frozen=True, slots=True)
class ReceivableProjectionPlan:
    """The complete, read-only plan for one pass. Shared by both modes."""

    window: ReceivableCohortWindow
    definition_seal: str
    membership_digest: str
    dispositions: tuple[CandidateDisposition, ...]
    orphaned_keys: tuple[str, ...]
    missing_keys: tuple[str, ...]
    planned_outcomes: dict[str, ApplyOutcome]
    source_fingerprint: str

    @property
    def positions(self) -> tuple[ObservedPosition, ...]:
        return tuple(
            item.position for item in self.dispositions if item.position is not None
        )

    def classification_counts(self) -> Counter[str]:
        counts: Counter[str] = Counter({item.value: 0 for item in CohortClassification})
        for item in self.dispositions:
            counts[item.classification.value] += 1
        return counts


@dataclass(frozen=True, slots=True)
class ReceivableProjectionResult:
    """Typed outcome of one pass, identical in shape for both modes.

    `run_id` is `None` for a dry run, and that is the only structural
    difference a caller sees. A dry run that reported a run id would be
    claiming durable evidence it never wrote.
    """

    mode: ProjectionMode
    run_kind: ReceivableProjectionRunKind
    run_id: uuid.UUID | None
    cohort_name: str
    cohort_definition_seal: str
    membership_digest: str
    cohort_count: int
    classification_counts: dict[str, int]
    inserted_count: int
    updated_count: int
    unchanged_count: int
    stale_skipped_count: int
    ambiguous_watermark_count: int
    orphaned_count: int
    missing_count: int
    currency_totals: dict[str, str]
    source_fingerprint: str
    result_fingerprint: str
    projection_version_low: int | None
    projection_version_high: int | None
    blockers: tuple[dict[str, str], ...]

    @property
    def covered_count(self) -> int:
        return self.classification_counts.get(CohortClassification.COVERED.value, 0)


@dataclass(frozen=True, slots=True)
class ParityRunEvidence:
    """Typed parity counts a parity pass asks to be recorded on its run row.

    A dataclass rather than a loose mapping because these numbers are evidence.
    An untyped bag lets a caller record `parity_matched_count` under a
    misspelled key and get a silent zero, which reads as "nothing matched"
    rather than as the bug it is.
    """

    matched_count: int = 0
    diverged_count: int = 0
    not_expressible_count: int = 0
    by_dimension: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconcileReceivableProjectionCommand:
    """Typed request for one projection pass.

    `mode` defaults to `DRY_RUN` on purpose. A caller that wants a write has to
    say so, and every adapter that forgets to pass a mode gets the safe one.
    """

    context: CommandContext
    window: ReceivableCohortWindow
    code_version: str
    database_schema_version: str
    run_kind: ReceivableProjectionRunKind = ReceivableProjectionRunKind.reconcile
    mode: ProjectionMode = ProjectionMode.DRY_RUN
    #: Parity counts merged into the persisted run row by the parity reporter.
    parity_evidence: ParityRunEvidence = field(default_factory=ParityRunEvidence)


# ── Derivation ──────────────────────────────────────────────────────────────


def _aware(value: datetime | None) -> datetime | None:
    """Normalise to UTC, treating a naive value as UTC.

    SQLite drops tzinfo on round-trip while PostgreSQL preserves it. Comparing
    the two without normalising is how a watermark comparison silently becomes
    a `TypeError` in one lane and a wrong answer in the other.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    aware = _aware(value)
    return None if aware is None else aware.isoformat()


def _money(value: object) -> Decimal:
    if value is None:
        return _ZERO
    return Decimal(str(value)).quantize(_ZERO)


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _id_digest(values: Sequence[uuid.UUID]) -> str:
    return digest_payload(sorted(str(item) for item in values))


def _newest(*values: datetime | None) -> datetime:
    """The newest of the contributing instants, or the epoch floor.

    The floor is never reached in practice — an invoice always has
    `updated_at` — but returning a real instant rather than `None` keeps the
    watermark column `NOT NULL` without a nullable comparison in the write path.
    """
    known = [item for item in (_aware(v) for v in values) if item is not None]
    if not known:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return max(known)


def service_scope_payload(subscription: Subscription) -> dict[str, object]:
    """The declared service scope of a position.

    Identity and commercial shape only. Operational facts that move without any
    commercial change — `last_seen_framed_ipv4`, `access_state` — are excluded
    on purpose: folding them in would make the service-scope dimension report
    a divergence every time a session re-established.
    """
    return {
        "offer_id": str(subscription.offer_id) if subscription.offer_id else None,
        "offer_version_id": (
            str(subscription.offer_version_id)
            if subscription.offer_version_id
            else None
        ),
        "service_address_id": (
            str(subscription.service_address_id)
            if subscription.service_address_id
            else None
        ),
        "bundle_id": str(subscription.bundle_id) if subscription.bundle_id else None,
        "billing_mode": _enum_value(subscription.billing_mode),
        "billing_cycle": _enum_value(subscription.billing_cycle),
        "contract_term": _enum_value(subscription.contract_term),
        "status": _enum_value(subscription.status),
        "start_at": _iso(subscription.start_at),
        "end_at": _iso(subscription.end_at),
        "next_billing_at": _iso(subscription.next_billing_at),
    }


def _effective_contract_version(
    db: Session, *, subscription_id: uuid.UUID, moment: datetime
) -> BillingContractVersion | None:
    """The one contract version effective for a subscription at `moment`.

    Returns `None` when zero or more than one answers. More-than-one is not
    resolved by picking the newest: ADR 0007 invariant 1 says exactly one is
    effective at an instant, so two is a finding for `billing.contracts`, not
    an input this projection may quietly disambiguate.
    """
    rows = (
        db.execute(
            select(BillingContractVersion)
            .where(BillingContractVersion.subscription_id == subscription_id)
            .where(
                BillingContractVersion.status.in_(
                    (
                        BillingContractVersionStatus.effective,
                        BillingContractVersionStatus.superseded,
                    )
                )
            )
            .where(BillingContractVersion.starts_at <= moment)
            .order_by(BillingContractVersion.version.desc())
        )
        .scalars()
        .all()
    )
    live = [
        row
        for row in rows
        if row.ends_at is None or (_aware(row.ends_at) or moment) > moment
    ]
    return live[0] if len(live) == 1 else None


def _covering_obligation(
    db: Session, *, subscription_id: uuid.UUID, period_start: datetime | None
) -> BillingObligation | None:
    """The ADR 0007 obligation covering the position's service period."""
    if period_start is None:
        return None
    rows = (
        db.execute(
            select(BillingObligation)
            .where(BillingObligation.subscription_id == subscription_id)
            .where(BillingObligation.period_start <= period_start)
            .where(BillingObligation.period_end > period_start)
            .order_by(BillingObligation.created_at.desc())
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


def _billing_treatment(
    db: Session, *, subscription_id: uuid.UUID, moment: datetime
) -> str:
    """Sub's authoritative non-standard billing treatment at `moment`.

    Read-only. `financial.subscription_billing_treatments` owns these rows; the
    projection observes them so the cadence dimension can refuse a comparison
    the pinned Subscriptions contract cannot express.
    """
    row = (
        db.execute(
            select(SubscriptionBillingArrangement)
            .where(SubscriptionBillingArrangement.subscription_id == subscription_id)
            .where(
                SubscriptionBillingArrangement.status == BillingTreatmentStatus.active
            )
            .where(SubscriptionBillingArrangement.starts_at <= moment)
            .order_by(SubscriptionBillingArrangement.starts_at.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        return "standard"
    ends_at = _aware(row.ends_at)
    if ends_at is not None and ends_at <= moment:
        return "standard"
    return _enum_value(row.treatment) or "standard"


def _derive_position(
    db: Session,
    *,
    invoice: Invoice,
    lines: Sequence[InvoiceLine],
    subscription: Subscription,
    lane: ReceivableLane,
) -> ObservedPosition:
    """Project one invoice into an observation. Reads only; writes nothing."""
    issued_at = (
        _aware(invoice.issued_at) or _aware(invoice.created_at) or datetime.now(UTC)
    )

    allocations = (
        db.execute(
            select(PaymentAllocation)
            .join(Payment, Payment.id == PaymentAllocation.payment_id)
            .where(PaymentAllocation.invoice_id == invoice.id)
            .where(PaymentAllocation.is_active.is_(True))
            .where(Payment.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    credit_applications = (
        db.execute(
            select(CreditNoteApplication).where(
                CreditNoteApplication.invoice_id == invoice.id
            )
        )
        .scalars()
        .all()
    )

    contract_version = _effective_contract_version(
        db, subscription_id=subscription.id, moment=issued_at
    )
    period_start = _aware(invoice.billing_period_start)
    obligation = _covering_obligation(
        db, subscription_id=subscription.id, period_start=period_start
    )
    treatment = _billing_treatment(
        db, subscription_id=subscription.id, moment=issued_at
    )
    expressible = treatment not in UNADOPTED_BILLING_TREATMENTS

    total = _money(invoice.total)
    outstanding = _money(invoice.balance_due)
    # Settled is derived from the incumbent's own two numbers rather than
    # re-summed from allocations. Re-summing here would make this a THIRD
    # derivation of settlement beside `resolve_invoice_settlement_amounts` and
    # the two collections planners, which is exactly what an observation must
    # not become.
    settled = total - outstanding
    if settled < _ZERO:
        settled = _ZERO

    scope_payload = service_scope_payload(subscription)
    scope_fingerprint = digest_payload(scope_payload)

    values: dict[str, object] = {
        "lane": lane.value,
        "cohort_name": COHORT_NAME,
        "cohort_definition_version": COHORT_DEFINITION_VERSION,
        "currency": (invoice.currency or "NGN").upper(),
        "observed_total_amount": total,
        "observed_settled_amount": settled,
        "observed_outstanding_amount": outstanding,
        "observed_invoice_status": _enum_value(invoice.status) or "unknown",
        "observed_issued_at": issued_at,
        "observed_due_at": _aware(invoice.due_at),
        "observed_paid_at": _aware(invoice.paid_at),
        "observed_period_start": period_start,
        "observed_period_end": _aware(invoice.billing_period_end),
        "observed_due_date_basis": _enum_value(invoice.due_date_basis),
        "observed_due_date_basis_ref": invoice.due_date_basis_ref,
        "observed_due_date_policy_version": invoice.due_date_policy_version,
        "contract_payment_terms_days": (
            contract_version.payment_terms_days if contract_version else None
        ),
        "service_scope_fingerprint": scope_fingerprint,
        "observed_offer_id": subscription.offer_id,
        "observed_offer_version_id": subscription.offer_version_id,
        "observed_service_address_id": subscription.service_address_id,
        "observed_bundle_id": subscription.bundle_id,
        "observed_billing_mode": _enum_value(subscription.billing_mode) or "unknown",
        "observed_billing_cycle": _enum_value(subscription.billing_cycle),
        "observed_subscription_status": (_enum_value(subscription.status) or "unknown"),
        "observed_collection_timing": (
            _enum_value(contract_version.collection_timing)
            if contract_version
            else None
        ),
        "observed_rate_basis": (
            _enum_value(contract_version.rate_basis) if contract_version else None
        ),
        "observed_rate_unit": (
            _enum_value(contract_version.rate_unit) if contract_version else None
        ),
        "observed_rate_quantity": (
            _money(contract_version.rate_quantity) if contract_version else None
        ),
        "observed_service_interval_unit": (
            _enum_value(contract_version.service_interval_unit)
            if contract_version
            else None
        ),
        "observed_service_interval_count": (
            contract_version.service_interval_count if contract_version else None
        ),
        "observed_invoice_interval_unit": (
            _enum_value(contract_version.invoice_interval_unit)
            if contract_version
            else None
        ),
        "observed_invoice_interval_count": (
            contract_version.invoice_interval_count if contract_version else None
        ),
        "observed_cadence_alignment": (
            _enum_value(contract_version.alignment) if contract_version else None
        ),
        "observed_anchor_day": (
            contract_version.anchor_day if contract_version else None
        ),
        "observed_end_of_month_rule": (
            _enum_value(contract_version.end_of_month_rule)
            if contract_version
            else None
        ),
        "observed_timezone_name": (
            contract_version.timezone_name if contract_version else None
        ),
        "observed_proration_policy": (
            _enum_value(contract_version.proration_policy) if contract_version else None
        ),
        "observed_billing_treatment": treatment,
        "billing_treatment_expressible": expressible,
    }

    line_digest = _id_digest([line.id for line in lines])
    allocation_digest = _id_digest([item.id for item in allocations])

    source_observed_at = _newest(
        invoice.updated_at,
        *[line.updated_at for line in lines],
        *[item.created_at for item in allocations],
        *[item.updated_at for item in credit_applications],
    )

    # The fingerprint of the SOURCE facts only. `projection_version`,
    # `projected_at` and `projected_by_run_id` are excluded: they change on
    # every rebuild by design, and folding them in would leave the projection
    # unable to prove it had reproduced anything.
    fingerprint_payload: dict[str, object] = {
        key: (
            _iso(value)
            if isinstance(value, datetime)
            else str(value)
            if value is not None
            else None
        )
        for key, value in sorted(values.items())
    }
    fingerprint_payload["invoice_id"] = str(invoice.id)
    fingerprint_payload["subscription_id"] = str(subscription.id)
    fingerprint_payload["account_id"] = str(invoice.account_id)
    fingerprint_payload["contract_version_id"] = (
        str(contract_version.id) if contract_version else None
    )
    fingerprint_payload["contract_source_version"] = (
        contract_version.source_version if contract_version else None
    )
    fingerprint_payload["obligation_id"] = str(obligation.id) if obligation else None
    fingerprint_payload["invoice_line_ids_sha256"] = line_digest
    fingerprint_payload["allocation_ids_sha256"] = allocation_digest
    fingerprint_payload["projection_policy_version"] = PROJECTION_POLICY_VERSION

    input_fingerprint = digest_payload(fingerprint_payload)
    source_fingerprint = digest_payload(
        {
            "input": input_fingerprint,
            "source_observed_at": source_observed_at.isoformat(),
        }
    )

    return ObservedPosition(
        receivable_key=receivable_key(invoice_id=str(invoice.id), lane=lane),
        lane=lane,
        invoice_id=invoice.id,
        account_id=invoice.account_id,
        subscription_id=subscription.id,
        contract_version_id=contract_version.id if contract_version else None,
        contract_source_version=(
            contract_version.source_version if contract_version else None
        ),
        obligation_id=obligation.id if obligation else None,
        source_observed_at=source_observed_at,
        source_fingerprint=source_fingerprint,
        input_row_fingerprint=input_fingerprint,
        invoice_line_ids_sha256=line_digest,
        allocation_ids_sha256=allocation_digest,
        service_scope_fingerprint=scope_fingerprint,
        values=values,
    )


# ── Planning (read-only; both modes share it) ───────────────────────────────


def _classify(
    db: Session, *, invoice: Invoice, window: ReceivableCohortWindow
) -> CandidateDisposition:
    status = _enum_value(invoice.status) or "unknown"
    if status in EXCLUDED_INVOICE_STATUSES:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.EXCLUDED_BY_STATUS,
            f"declared exclusion: status={status}",
        )
    if status not in DECLARED_INVOICE_STATUSES:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.UNRESOLVED,
            f"status {status!r} is outside the declared cohort vocabulary",
        )
    issued_at = _aware(invoice.issued_at)
    if issued_at is None or not window.contains(issued_at):
        return CandidateDisposition(
            invoice.id,
            CohortClassification.UNRESOLVED,
            "issued_at is absent or outside the observation window",
        )

    lines = (
        db.execute(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice.id)
            .where(InvoiceLine.is_active.is_(True))
        )
        .scalars()
        .all()
    )
    linked = [line for line in lines if line.subscription_id is not None]
    if not linked:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.UNEXPECTED_UNLINKED,
            "no active invoice line carries a subscription_id",
        )
    subscription_ids = {line.subscription_id for line in linked}
    if len(subscription_ids) > 1:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.AMBIGUOUS,
            f"lines name {len(subscription_ids)} subscriptions; exactly one required",
        )

    subscription = db.get(Subscription, next(iter(subscription_ids)))
    if subscription is None:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.UNRESOLVED,
            "the linked subscription no longer exists",
        )
    lane = lane_for_billing_mode(_enum_value(subscription.billing_mode) or "")
    if lane is None:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.UNRESOLVED,
            f"billing_mode {_enum_value(subscription.billing_mode)!r} has no lane",
        )

    position = _derive_position(
        db, invoice=invoice, lines=linked, subscription=subscription, lane=lane
    )
    if position.contract_version_id is None and position.obligation_id is None:
        return CandidateDisposition(
            invoice.id,
            CohortClassification.NOT_EXPRESSIBLE,
            (
                "no effective contract version and no covering obligation, so "
                "the ADR 0007 dimensions have no counterparty to compare"
            ),
            position,
        )
    return CandidateDisposition(
        invoice.id, CohortClassification.COVERED, "resolved", position
    )


def plan_receivable_projection(
    db: Session, command: ReconcileReceivableProjectionCommand
) -> ReceivableProjectionPlan:
    """Build the complete pass plan. Strictly read-only.

    Both modes call exactly this. A dry run that used a different derivation
    from the apply path would be a rehearsal of a program nobody runs.
    """
    window = command.window.as_utc()
    candidates = (
        db.execute(
            select(Invoice)
            .where(Invoice.is_active.is_(True))
            .where(Invoice.created_at >= window.window_start)
            .where(Invoice.created_at < window.window_end)
            .where(Invoice.created_at <= window.cutoff_at)
            .order_by(Invoice.id)
        )
        .scalars()
        .all()
    )

    dispositions: list[CandidateDisposition] = []
    seen_keys: set[str] = set()
    for invoice in candidates:
        disposition = _classify(db, invoice=invoice, window=window)
        position = disposition.position
        if position is not None:
            if position.receivable_key in seen_keys:
                disposition = CandidateDisposition(
                    invoice.id,
                    CohortClassification.DUPLICATE,
                    f"receivable_key {position.receivable_key} already claimed",
                )
            else:
                seen_keys.add(position.receivable_key)
        dispositions.append(disposition)

    stored = {
        row.receivable_key: row
        for row in db.execute(select(BillingReceivableProjection)).scalars()
    }

    planned: dict[str, ApplyOutcome] = {}
    for disposition in dispositions:
        position = disposition.position
        if position is None:
            continue
        existing = stored.get(position.receivable_key)
        if existing is None:
            planned[position.receivable_key] = ApplyOutcome.INSERTED
            continue
        stored_at = _aware(existing.source_observed_at)
        if stored_at is not None and stored_at > position.source_observed_at:
            planned[position.receivable_key] = ApplyOutcome.STALE_SKIPPED
        elif stored_at == position.source_observed_at:
            planned[position.receivable_key] = (
                ApplyOutcome.UNCHANGED
                if existing.source_fingerprint == position.source_fingerprint
                else ApplyOutcome.AMBIGUOUS_WATERMARK
            )
        else:
            planned[position.receivable_key] = ApplyOutcome.UPDATED

    live_keys = set(planned)
    seal = definition_seal(window)
    # An orphan is a row THIS sealed cohort used to contain and no longer does
    # — an invoice deactivated, voided, or unlinked since it was projected.
    # Rows carrying a different seal belong to another window and are simply out
    # of scope for this pass; counting them would make every routine reconcile
    # of one month report every other month as drift.
    same_seal = {
        key for key, row in stored.items() if row.cohort_definition_seal == seal
    }
    orphaned = tuple(sorted(same_seal - live_keys))
    missing = tuple(
        sorted(
            key for key, outcome in planned.items() if outcome is ApplyOutcome.INSERTED
        )
    )

    source_fingerprint = digest_payload(
        [
            [item.position.receivable_key, item.position.source_fingerprint]
            for item in dispositions
            if item.position is not None
        ]
    )
    return ReceivableProjectionPlan(
        window=window,
        definition_seal=seal,
        membership_digest=membership_digest(live_keys),
        dispositions=tuple(dispositions),
        orphaned_keys=orphaned,
        missing_keys=missing,
        planned_outcomes=planned,
        source_fingerprint=source_fingerprint,
    )


# ── Writing ─────────────────────────────────────────────────────────────────


def _acquire_lock(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _RECONCILE_LOCK_KEY}
        )


def _next_projection_version(db: Session) -> int:
    """Allocate the next globally monotonic projection version.

    PostgreSQL uses the sequence, which is monotonic across concurrent workers
    and across transactions. The SQLite fallback is `max + 1` and is monotonic
    only within the fast unit lane's single process — that lane is explicitly
    non-authoritative for concurrency, and describing its result as such would
    be a false claim.
    """
    if db.get_bind().dialect.name == "postgresql":
        value = db.execute(
            text(f"SELECT nextval('{PROJECTION_VERSION_SEQUENCE}')")
        ).scalar_one()
        return int(value)
    current = db.execute(
        select(
            func.coalesce(func.max(BillingReceivableProjection.projection_version), 0)
        )
    ).scalar_one()
    return int(current) + 1


def _row_values(
    position: ObservedPosition,
    *,
    run_id: uuid.UUID,
    projection_version: int,
    seal: str,
    stamp: datetime,
) -> dict[str, object]:
    values = dict(position.values)
    values.update(
        {
            "receivable_key": position.receivable_key,
            "cohort_definition_seal": seal,
            "projection_version": projection_version,
            "source_observed_at": position.source_observed_at,
            "source_fingerprint": position.source_fingerprint,
            "projected_at": stamp,
            "projection_policy_version": PROJECTION_POLICY_VERSION,
            "projected_by_run_id": run_id,
            "invoice_id": position.invoice_id,
            "account_id": position.account_id,
            "subscription_id": position.subscription_id,
            "contract_version_id": position.contract_version_id,
            "contract_source_version": position.contract_source_version,
            "obligation_id": position.obligation_id,
            "invoice_line_ids_sha256": position.invoice_line_ids_sha256,
            "allocation_ids_sha256": position.allocation_ids_sha256,
            "input_row_fingerprint": position.input_row_fingerprint,
        }
    )
    return values


def _write_position(
    db: Session,
    position: ObservedPosition,
    *,
    planned: ApplyOutcome,
    run_id: uuid.UUID,
    seal: str,
    stamp: datetime,
) -> tuple[ApplyOutcome, int | None]:
    """Apply one planned observation under the staleness predicate.

    Returns the outcome and, when a row was written, the projection version it
    now carries — read back from the statement rather than re-queried, so the
    number reported is the one the database actually stored.
    """
    if planned in (ApplyOutcome.UNCHANGED, ApplyOutcome.STALE_SKIPPED):
        return planned, None
    if planned is ApplyOutcome.AMBIGUOUS_WATERMARK:
        logger.warning(
            "receivable_projection: equal watermark with a different "
            "fingerprint for %s; refusing to guess",
            position.receivable_key,
        )
        return planned, None

    version = _next_projection_version(db)
    values = _row_values(
        position, run_id=run_id, projection_version=version, seal=seal, stamp=stamp
    )
    table = cast("Table", BillingReceivableProjection.__table__)

    if db.get_bind().dialect.name == "postgresql":
        insert_statement = pg_insert(table).values(
            id=uuid.uuid4(), created_at=stamp, **values
        )
        update_columns = {
            key: insert_statement.excluded[key]
            for key in values
            if key not in {"receivable_key"}
        }
        upsert = insert_statement.on_conflict_do_update(
            index_elements=[table.c.receivable_key],
            set_=update_columns,
            # The predicate that closes the gap between the plan's read and
            # this write. A concurrent pass that already stored a newer
            # observation makes this a no-op instead of a regression.
            where=insert_statement.excluded.source_observed_at
            > table.c.source_observed_at,
        ).returning(table.c.projection_version)
        wrote = db.execute(upsert).scalar_one_or_none()
        if wrote is None:
            # A concurrent pass stored a newer observation between the plan's
            # read and this write. Converging, not failing.
            return ApplyOutcome.STALE_SKIPPED, None
        return planned, int(wrote)

    existing = (
        db.execute(
            select(BillingReceivableProjection).where(
                BillingReceivableProjection.receivable_key == position.receivable_key
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        db.add(BillingReceivableProjection(id=uuid.uuid4(), created_at=stamp, **values))
        # Flush so the next allocation in this same pass sees the row. Without
        # it the max(...) fallback would hand two positions the same version.
        db.flush()
        return ApplyOutcome.INSERTED, version
    stored_at = _aware(existing.source_observed_at)
    if stored_at is not None and stored_at >= position.source_observed_at:
        return ApplyOutcome.STALE_SKIPPED, None
    for key, value in values.items():
        setattr(existing, key, value)
    db.flush()
    return ApplyOutcome.UPDATED, version


def _currency_totals(plan: ReceivableProjectionPlan) -> dict[str, str]:
    """Outstanding, summed per currency. Never summed across them.

    Reported as strings so a JSON round-trip cannot turn exact money into a
    float (ADR 0007 invariant 13 forbids nominal cross-currency comparison, and
    a float total would quietly break the exactness the invariant assumes).
    """
    totals: dict[str, Decimal] = {}
    for position in plan.positions:
        totals[position.currency] = totals.get(position.currency, _ZERO) + (
            position.outstanding
        )
    return {currency: str(value) for currency, value in sorted(totals.items())}


def _blocker_payload() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "code": blocker.code,
            "dimension": blocker.dimension.value,
            "reason": blocker.reason.value,
            "statement": blocker.statement,
            "pinned_package": blocker.pinned_package,
            "pinned_version": blocker.pinned_version,
            "pinned_revision": blocker.pinned_revision,
        }
        for blocker in STANDING_BLOCKERS
    )


def _result(
    *,
    command: ReconcileReceivableProjectionCommand,
    plan: ReceivableProjectionPlan,
    outcomes: Counter[str],
    run_id: uuid.UUID | None,
    version_low: int | None,
    version_high: int | None,
) -> ReceivableProjectionResult:
    counts = plan.classification_counts()
    currency_totals = _currency_totals(plan)
    result_fingerprint = digest_payload(
        {
            "classification": dict(sorted(counts.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "currency_totals": currency_totals,
            "membership_digest": plan.membership_digest,
            "definition_seal": plan.definition_seal,
            "orphaned": list(plan.orphaned_keys),
        }
    )
    return ReceivableProjectionResult(
        mode=command.mode,
        run_kind=command.run_kind,
        run_id=run_id,
        cohort_name=COHORT_NAME,
        cohort_definition_seal=plan.definition_seal,
        membership_digest=plan.membership_digest,
        cohort_count=len(plan.dispositions),
        classification_counts=dict(sorted(counts.items())),
        inserted_count=outcomes.get(ApplyOutcome.INSERTED.value, 0),
        updated_count=outcomes.get(ApplyOutcome.UPDATED.value, 0),
        unchanged_count=outcomes.get(ApplyOutcome.UNCHANGED.value, 0),
        stale_skipped_count=outcomes.get(ApplyOutcome.STALE_SKIPPED.value, 0),
        ambiguous_watermark_count=outcomes.get(
            ApplyOutcome.AMBIGUOUS_WATERMARK.value, 0
        ),
        orphaned_count=len(plan.orphaned_keys),
        missing_count=len(plan.missing_keys),
        currency_totals=currency_totals,
        source_fingerprint=plan.source_fingerprint,
        result_fingerprint=result_fingerprint,
        projection_version_low=version_low,
        projection_version_high=version_high,
        blockers=_blocker_payload(),
    )


def _validate(command: ReconcileReceivableProjectionCommand) -> None:
    if not command.context.idempotency_key:
        raise ReceivableProjectionError(
            code=f"{OWNER}.missing_idempotency_key",
            message="A projection pass requires an explicit idempotency key.",
            details={"field": "context.idempotency_key"},
        )
    for label, value in (
        ("code_version", command.code_version),
        ("database_schema_version", command.database_schema_version),
    ):
        if not value or not value.strip():
            raise ReceivableProjectionError(
                code=f"{OWNER}.incomplete_run_identity",
                message="A projection run must record its exact code and schema version.",
                details={"field": label},
            )


def _open_run(
    db: Session,
    *,
    command: ReconcileReceivableProjectionCommand,
    plan: ReceivableProjectionPlan,
    run_id: uuid.UUID,
    stamp: datetime,
) -> ReceivableProjectionRun:
    """Insert the run row before any observation can point at it.

    Every projected row carries `projected_by_run_id` as a RESTRICT foreign
    key, so the run must exist first. The counts and the result fingerprint are
    not knowable yet; they are filled in by `_close_run` inside this same owner
    transaction, so a half-filled run row is never visible to anything and a
    failed pass leaves none behind at all.

    The placeholder fingerprint is a syntactically valid digest so the row
    satisfies its CHECK constraint on the way in. It is overwritten before
    commit, and `test_the_run_row_carries_the_cutover_evidence_fields` would
    fail loudly if it ever survived.
    """
    run = ReceivableProjectionRun(
        id=run_id,
        run_kind=command.run_kind,
        cohort_name=COHORT_NAME,
        cohort_definition_version=COHORT_DEFINITION_VERSION,
        cohort_definition_seal=plan.definition_seal,
        membership_digest=plan.membership_digest,
        projection_policy_version=PROJECTION_POLICY_VERSION,
        evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
        cutoff_at=plan.window.cutoff_at,
        observation_started_at=plan.window.window_start,
        observation_ended_at=plan.window.window_end,
        cohort_count=len(plan.dispositions),
        covered_count=0,
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        excluded_by_status_count=0,
        not_expressible_count=0,
        parity_by_dimension={},
        blockers=[],
        currency_totals={},
        cohort_classification={},
        source_fingerprint=plan.source_fingerprint,
        result_fingerprint=_PENDING_FINGERPRINT,
        code_version=command.code_version,
        database_schema_version=command.database_schema_version,
        idempotency_key=command.context.idempotency_key or "",
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        actor=command.context.actor,
        reason=command.context.reason,
        created_at=stamp,
    )
    db.add(run)
    db.flush()
    return run


def _close_run(
    run: ReceivableProjectionRun,
    *,
    command: ReconcileReceivableProjectionCommand,
    plan: ReceivableProjectionPlan,
    result: ReceivableProjectionResult,
) -> None:
    """Complete the run row with what only the finished pass knows."""
    counts = result.classification_counts
    run.covered_count = counts.get(CohortClassification.COVERED.value, 0)
    run.unresolved_count = counts.get(CohortClassification.UNRESOLVED.value, 0)
    run.ambiguous_count = counts.get(CohortClassification.AMBIGUOUS.value, 0)
    run.unexpected_unlinked_count = counts.get(
        CohortClassification.UNEXPECTED_UNLINKED.value, 0
    )
    run.duplicate_count = counts.get(CohortClassification.DUPLICATE.value, 0)
    run.excluded_by_status_count = counts.get(
        CohortClassification.EXCLUDED_BY_STATUS.value, 0
    )
    run.not_expressible_count = counts.get(
        CohortClassification.NOT_EXPRESSIBLE.value, 0
    )
    run.inserted_count = result.inserted_count
    run.updated_count = result.updated_count
    run.unchanged_count = result.unchanged_count
    run.stale_skipped_count = result.stale_skipped_count
    run.ambiguous_watermark_count = result.ambiguous_watermark_count
    run.orphaned_count = result.orphaned_count
    run.missing_count = result.missing_count
    run.parity_matched_count = command.parity_evidence.matched_count
    run.parity_diverged_count = command.parity_evidence.diverged_count
    run.parity_not_expressible_count = command.parity_evidence.not_expressible_count
    run.parity_by_dimension = dict(command.parity_evidence.by_dimension)
    run.blockers = [dict(item) for item in result.blockers]
    run.currency_totals = dict(result.currency_totals)
    run.cohort_classification = {
        "counts": counts,
        # Only the non-covered dispositions carry a detail worth storing:
        # "resolved" repeated once per covered row is noise, and the covered
        # count already says how many there were.
        "details": [
            {
                "invoice_id": str(item.invoice_id),
                "classification": item.classification.value,
                "detail": item.detail,
            }
            for item in plan.dispositions
            if item.classification is not CohortClassification.COVERED
        ],
        "orphaned_keys": list(plan.orphaned_keys),
    }
    run.projection_version_low = result.projection_version_low
    run.projection_version_high = result.projection_version_high
    run.result_fingerprint = result.result_fingerprint


def _apply(
    db: Session, command: ReconcileReceivableProjectionCommand
) -> ReceivableProjectionResult:
    _acquire_lock(db)
    plan = plan_receivable_projection(db, command)
    stamp = datetime.now(UTC)
    run_id = uuid.uuid4()

    # The run row is inserted BEFORE any projected row, because every
    # observation carries `projected_by_run_id` as a RESTRICT foreign key: an
    # observation whose run does not yet exist has no provenance to point at.
    # Its counts and result fingerprint are completed further down, inside this
    # same owner transaction, so a partially filled run row is never visible to
    # anything and a failed pass leaves none behind.
    run = _open_run(db, command=command, plan=plan, run_id=run_id, stamp=stamp)

    outcomes: Counter[str] = Counter()
    versions: list[int] = []
    for disposition in plan.dispositions:
        position = disposition.position
        if position is None:
            continue
        planned = plan.planned_outcomes[position.receivable_key]
        outcome, version = _write_position(
            db,
            position,
            planned=planned,
            run_id=run_id,
            seal=plan.definition_seal,
            stamp=stamp,
        )
        outcomes[outcome.value] += 1
        if version is not None:
            versions.append(version)

    result = _result(
        command=command,
        plan=plan,
        outcomes=outcomes,
        run_id=run_id,
        version_low=min(versions) if versions else None,
        version_high=max(versions) if versions else None,
    )
    _close_run(run, command=command, plan=plan, result=result)
    emit_event(
        db,
        EventType.receivable_projection_reconciled,
        {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "causation_id": (
                str(command.context.causation_id)
                if command.context.causation_id is not None
                else None
            ),
            "idempotency_key": command.context.idempotency_key,
            "aggregate_type": "receivable_projection_run",
            "aggregate_id": str(run_id),
            "aggregate_version": str(command.context.command_id),
            "scope": command.context.scope,
            "reason": command.context.reason,
            "run_kind": command.run_kind.value,
            "cohort_name": COHORT_NAME,
            "cohort_definition_seal": plan.definition_seal,
            "membership_digest": plan.membership_digest,
            "cohort_count": result.cohort_count,
            "covered_count": result.covered_count,
            "inserted_count": result.inserted_count,
            "updated_count": result.updated_count,
            "stale_skipped_count": result.stale_skipped_count,
            "ambiguous_watermark_count": result.ambiguous_watermark_count,
            "orphaned_count": result.orphaned_count,
            "result_fingerprint": result.result_fingerprint,
            "authority_moved": False,
        },
        actor=command.context.actor,
    )
    logger.info(
        "receivable_projection %s: %d covered, %d inserted, %d updated, "
        "%d stale-skipped, %d orphaned",
        command.run_kind.value,
        result.covered_count,
        result.inserted_count,
        result.updated_count,
        result.stale_skipped_count,
        result.orphaned_count,
    )
    return result


def reconcile_receivable_projection(
    db: Session, command: ReconcileReceivableProjectionCommand
) -> ReceivableProjectionResult:
    """Run one projection pass. Dry run by default; idempotent either way.

    In `DRY_RUN` the owner-command boundary is never entered and the session is
    never written to, so the caller may hold an open read transaction. In
    `APPLY` the session must have no active caller transaction: the owner
    boundary commits or rolls back the whole pass before returning.
    """
    _validate(command)
    if command.mode is ProjectionMode.DRY_RUN:
        plan = plan_receivable_projection(db, command)
        planned_counts: Counter[str] = Counter(
            outcome.value for outcome in plan.planned_outcomes.values()
        )
        return _result(
            command=command,
            plan=plan,
            outcomes=planned_counts,
            run_id=None,
            version_low=None,
            version_high=None,
        )

    def operation() -> ReceivableProjectionResult:
        return _apply(db, command)

    return execute_owner_command(
        db,
        definition=_RECONCILE_COMMAND,
        context=command.context,
        operation=operation,
    )


__all__ = [
    "OWNER",
    "ApplyOutcome",
    "CandidateDisposition",
    "ObservedPosition",
    "ParityRunEvidence",
    "ProjectionMode",
    "ReceivableProjectionError",
    "ReceivableProjectionPlan",
    "ReceivableProjectionResult",
    "ReconcileReceivableProjectionCommand",
    "plan_receivable_projection",
    "reconcile_receivable_projection",
    "service_scope_payload",
]
