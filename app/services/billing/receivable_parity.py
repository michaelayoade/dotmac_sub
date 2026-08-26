"""Read-only semantic parity over the `receivable-shadow-01` cohort.

Seven dimensions, evaluated and reported independently: cadence, proration,
obligations, settlements, receivable amount, due-date provenance, and service
scope. A single aggregate verdict would hide which half of the chain
disagrees, and the two halves have different owners.

## This module writes nothing

It opens no transaction, constructs no model, and calls no command owner. It
reads the projection beside the incumbent facts and returns a typed report.
The report is *recorded* — if the operator asks for it — by
`billing.receivable_projection`, which is the one writer of the run row.

## Three outcomes, never two

`MATCHED`, `DIVERGED`, and `NOT_EXPRESSIBLE`. The third is the point. Folding
"we cannot compare this" into either of the first two is how a parity claim
comes to cover less than it appears to:

* the Subscriptions a3 treatment contract is schema-composed but not admitted
  to the application runtime, so a complimentary or sponsored cadence verdict
  is `NOT_EXPRESSIBLE` with the release coordinates attached;
* a position with no ADR 0007 obligation has no counterparty, so the
  obligations dimension is `NOT_EXPRESSIBLE` rather than a divergence blamed on
  a row that was never written;
* an invoice whose due-date basis is absent or `unknown_unverified` is a lawful
  historical observation that cannot drive collection. Comparing it to a
  contract-derived expectation would manufacture a verdict out of an input the
  incumbent itself refuses to act on.

## What the due-date dimension is actually measuring

`BillingContractVersion.payment_terms_days` exists and is populated, and
nothing computes `invoices.due_at` from it: the seven live issuance sites each
resolve their own day count through `resolve_payment_due_days`. So a divergence
here is expected on the current tree. It is reported, counted, and left alone —
repairing it would move authority, which this task does not do.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice
from app.models.billing_contract import BillingContractVersion, BillingObligation
from app.models.billing_receivable_projection import BillingReceivableProjection
from app.models.catalog import BillingCycle, Subscription
from app.services.billing._common import resolve_invoice_settlement_amounts
from app.services.billing.receivable_cohort import (
    STANDING_BLOCKERS,
    CohortClassification,
    NotExpressibleReason,
    ParityDimension,
    ParityOutcome,
    ReceivableCohortWindow,
    definition_seal,
    digest_payload,
)
from app.services.billing.receivable_projection import (
    ParityRunEvidence,
    ProjectionMode,
    ReconcileReceivableProjectionCommand,
    plan_receivable_projection,
    service_scope_payload,
)
from app.services.owner_commands import CommandContext

_ZERO = Decimal("0.0000")
#: Money tolerance. Exactly zero: the incumbent stores money as `Numeric`, both
#: sides are exact decimals, and a tolerance here would be a place for a real
#: rounding defect to hide.
_MONEY_TOLERANCE = Decimal("0")
#: Due-date tolerance. One day, because the incumbent computes due dates from a
#: whole-day term while the contract expresses them the same way; a sub-day
#: difference is a timezone artefact rather than a terms disagreement.
_DUE_TOLERANCE = timedelta(days=1)

#: Cadence equivalence between the subscription's declared cycle and the
#: contract version's invoice interval. Calendar terms, never day counts
#: (ADR 0007 invariant 6).
_CYCLE_INTERVALS: dict[str, tuple[str, int]] = {
    BillingCycle.daily.value: ("day", 1),
    BillingCycle.weekly.value: ("week", 1),
    BillingCycle.monthly.value: ("month", 1),
    BillingCycle.quarterly.value: ("month", 3),
    BillingCycle.annual.value: ("year", 1),
}


@dataclass(frozen=True, slots=True)
class DimensionVerdict:
    """One dimension's conclusion for one position."""

    dimension: ParityDimension
    outcome: ParityOutcome
    detail: str
    reason: NotExpressibleReason | None = None


@dataclass(frozen=True, slots=True)
class PositionParity:
    """Every dimension's verdict for one receivable position."""

    receivable_key: str
    verdicts: tuple[DimensionVerdict, ...]

    def outcome_counts(self) -> Counter[str]:
        return Counter(verdict.outcome.value for verdict in self.verdicts)


@dataclass(frozen=True, slots=True)
class ReceivableParityReport:
    """The complete read-only parity result for one sealed cohort."""

    cohort_definition_seal: str
    evaluated_count: int
    unprojected_count: int
    matched_count: int
    diverged_count: int
    not_expressible_count: int
    by_dimension: dict[str, dict[str, int]]
    not_expressible_reasons: dict[str, int]
    positions: tuple[PositionParity, ...]
    blockers: tuple[dict[str, str], ...]
    report_fingerprint: str

    def as_run_evidence(self) -> ParityRunEvidence:
        """The typed subset `billing.receivable_projection` records on a run."""
        return ParityRunEvidence(
            matched_count=self.matched_count,
            diverged_count=self.diverged_count,
            not_expressible_count=self.not_expressible_count,
            by_dimension={
                "dimensions": self.by_dimension,
                "not_expressible_reasons": self.not_expressible_reasons,
                "unprojected_count": self.unprojected_count,
                "report_fingerprint": self.report_fingerprint,
            },
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _decimal(value: object) -> Decimal:
    return _ZERO if value is None else Decimal(str(value)).quantize(_ZERO)


def _blocked(
    dimension: ParityDimension, reason: NotExpressibleReason, detail: str
) -> DimensionVerdict:
    return DimensionVerdict(dimension, ParityOutcome.NOT_EXPRESSIBLE, detail, reason)


def _verdict(
    dimension: ParityDimension, *, matched: bool, detail: str
) -> DimensionVerdict:
    return DimensionVerdict(
        dimension,
        ParityOutcome.MATCHED if matched else ParityOutcome.DIVERGED,
        detail,
    )


# ── The seven dimensions ────────────────────────────────────────────────────


def _cadence(
    row: BillingReceivableProjection, version: BillingContractVersion | None
) -> DimensionVerdict:
    if not row.billing_treatment_expressible:
        return _blocked(
            ParityDimension.CADENCE,
            NotExpressibleReason.SUBSCRIPTION_BILLING_TREATMENT_NOT_ADOPTED,
            (
                f"treatment {row.observed_billing_treatment!r} is not mapped to "
                "the schema-composed Subscriptions runtime contract"
            ),
        )
    if version is None:
        return _blocked(
            ParityDimension.CADENCE,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            "no effective contract version at the invoice issue instant",
        )
    cycle = row.observed_billing_cycle
    if cycle is None:
        return _blocked(
            ParityDimension.CADENCE,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            (
                "the subscription inherits its cadence from the offer version, "
                "which this projection does not carry"
            ),
        )
    expected = _CYCLE_INTERVALS.get(cycle)
    if expected is None:
        return _blocked(
            ParityDimension.CADENCE,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            f"billing cycle {cycle!r} has no declared interval equivalence",
        )
    actual = (
        str(getattr(version.invoice_interval_unit, "value", "")),
        int(version.invoice_interval_count or 0),
    )
    return _verdict(
        ParityDimension.CADENCE,
        matched=actual == expected,
        detail=f"subscription {cycle} -> {expected}; contract -> {actual}",
    )


def _proration(
    row: BillingReceivableProjection, version: BillingContractVersion | None
) -> DimensionVerdict:
    if version is None:
        return _blocked(
            ParityDimension.PRORATION,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            "no effective contract version to declare a proration policy",
        )
    declared = row.observed_proration_policy
    contracted = str(getattr(version.proration_policy, "value", ""))
    return _verdict(
        ParityDimension.PRORATION,
        matched=declared == contracted,
        detail=f"projected {declared!r} vs contracted {contracted!r}",
    )


def _obligations(
    row: BillingReceivableProjection, obligation: BillingObligation | None
) -> DimensionVerdict:
    if obligation is None:
        return _blocked(
            ParityDimension.OBLIGATIONS,
            NotExpressibleReason.NO_SHADOW_OBLIGATION_IN_WINDOW,
            "no ADR 0007 obligation covers this subscription and period",
        )
    if (obligation.currency or "").upper() != (row.currency or "").upper():
        return _blocked(
            ParityDimension.OBLIGATIONS,
            NotExpressibleReason.MIXED_CURRENCY_POSITION,
            (
                f"obligation currency {obligation.currency!r} differs from "
                f"invoice currency {row.currency!r}; nominal comparison refused"
            ),
        )
    gross = _decimal(obligation.gross_amount)
    total = _decimal(row.observed_total_amount)
    return _verdict(
        ParityDimension.OBLIGATIONS,
        matched=abs(gross - total) <= _MONEY_TOLERANCE,
        detail=f"obligation gross {gross} vs invoice total {total}",
    )


def _settlements(db: Session, row: BillingReceivableProjection) -> DimensionVerdict:
    """Compare the projected settled amount with the incumbent resolver.

    `resolve_invoice_settlement_amounts` is the canonical read of settlement
    for `financial.invoices` and `financial.payments`. Calling it is a READ of
    the owner, which is exactly right: re-summing allocations here would make
    this module a competing derivation of the very number it is checking.
    """
    resolved = resolve_invoice_settlement_amounts(db, row.invoice_id)
    projected = _decimal(row.observed_settled_amount)
    applied = _decimal(resolved.total_applied)
    return _verdict(
        ParityDimension.SETTLEMENTS,
        matched=abs(projected - applied) <= _MONEY_TOLERANCE,
        detail=(
            f"projected settled {projected} vs resolver "
            f"{applied} (payments {resolved.payments_applied}, "
            f"credits {resolved.credits_applied}, "
            f"opening funding {resolved.opening_funding_applied})"
        ),
    )


def _receivable_amount(
    db: Session, row: BillingReceivableProjection, invoice: Invoice | None
) -> DimensionVerdict:
    if invoice is None:
        return _blocked(
            ParityDimension.RECEIVABLE_AMOUNT,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            "the incumbent invoice row is no longer present",
        )
    if (invoice.currency or "").upper() != (row.currency or "").upper():
        return _blocked(
            ParityDimension.RECEIVABLE_AMOUNT,
            NotExpressibleReason.MIXED_CURRENCY_POSITION,
            "invoice currency changed after projection; nominal comparison refused",
        )
    projected = _decimal(row.observed_outstanding_amount)
    incumbent = _decimal(invoice.balance_due)
    return _verdict(
        ParityDimension.RECEIVABLE_AMOUNT,
        matched=abs(projected - incumbent) <= _MONEY_TOLERANCE,
        detail=(
            f"projected outstanding {projected} vs incumbent balance_due "
            f"{incumbent} (an observation of the incumbent's own number, not a "
            "competing derivation)"
        ),
    )


def _due_date_provenance(
    row: BillingReceivableProjection, version: BillingContractVersion | None
) -> DimensionVerdict:
    basis = row.observed_due_date_basis
    if basis is None or basis == "unknown_unverified":
        return _blocked(
            ParityDimension.DUE_DATE_PROVENANCE,
            NotExpressibleReason.UNVERIFIED_DUE_DATE_PROVENANCE,
            f"due-date basis is {basis!r}; the incumbent will not collect on it",
        )
    if version is None:
        return _blocked(
            ParityDimension.DUE_DATE_PROVENANCE,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            "no contract version to supply the expected payment terms",
        )
    issued_at = _aware(row.observed_issued_at)
    due_at = _aware(row.observed_due_at)
    if issued_at is None or due_at is None:
        return _blocked(
            ParityDimension.DUE_DATE_PROVENANCE,
            NotExpressibleReason.UNVERIFIED_DUE_DATE_PROVENANCE,
            "issued_at or due_at is absent on a basis that claims to be verified",
        )
    terms = int(version.payment_terms_days or 0)
    expected = issued_at + timedelta(days=terms)
    return _verdict(
        ParityDimension.DUE_DATE_PROVENANCE,
        matched=abs(due_at - expected) <= _DUE_TOLERANCE,
        detail=(
            f"basis={basis} ref={row.observed_due_date_basis_ref!r} "
            f"policy={row.observed_due_date_policy_version!r}; observed due "
            f"{due_at.isoformat()} vs contract terms {terms}d -> "
            f"{expected.isoformat()}"
        ),
    )


def _service_scope(
    row: BillingReceivableProjection, subscription: Subscription | None
) -> DimensionVerdict:
    if subscription is None:
        return _blocked(
            ParityDimension.SERVICE_SCOPE,
            NotExpressibleReason.NO_EFFECTIVE_CONTRACT_VERSION,
            "the projected subscription no longer exists",
        )
    live = digest_payload(service_scope_payload(subscription))
    return _verdict(
        ParityDimension.SERVICE_SCOPE,
        matched=live == row.service_scope_fingerprint,
        detail=(
            "projected service-scope fingerprint "
            f"{row.service_scope_fingerprint[:12]} vs live {live[:12]}"
        ),
    )


# ── The report ──────────────────────────────────────────────────────────────


def _evaluate_row(db: Session, row: BillingReceivableProjection) -> PositionParity:
    version = (
        db.get(BillingContractVersion, row.contract_version_id)
        if row.contract_version_id
        else None
    )
    obligation = (
        db.get(BillingObligation, row.obligation_id) if row.obligation_id else None
    )
    invoice = db.get(Invoice, row.invoice_id)
    subscription = db.get(Subscription, row.subscription_id)
    return PositionParity(
        receivable_key=row.receivable_key,
        verdicts=(
            _cadence(row, version),
            _proration(row, version),
            _obligations(row, obligation),
            _settlements(db, row),
            _receivable_amount(db, row, invoice),
            _due_date_provenance(row, version),
            _service_scope(row, subscription),
        ),
    )


def evaluate_receivable_parity(
    db: Session,
    *,
    window: ReceivableCohortWindow,
    context: CommandContext,
    code_version: str,
    database_schema_version: str,
) -> ReceivableParityReport:
    """Evaluate every projected position in the sealed cohort. Writes nothing.

    `unprojected_count` is reported separately from the parity outcomes: a
    cohort member with no projected row has not "failed parity", it has not
    been projected, and merging the two counts would let an unbuilt projection
    read as a clean comparison.
    """
    plan = plan_receivable_projection(
        db,
        ReconcileReceivableProjectionCommand(
            context=context,
            window=window,
            code_version=code_version,
            database_schema_version=database_schema_version,
            mode=ProjectionMode.DRY_RUN,
        ),
    )
    member_keys = {
        item.position.receivable_key
        for item in plan.dispositions
        if item.position is not None
        and item.classification
        in (CohortClassification.COVERED, CohortClassification.NOT_EXPRESSIBLE)
    }
    rows = {
        row.receivable_key: row
        for row in db.execute(
            select(BillingReceivableProjection).where(
                BillingReceivableProjection.receivable_key.in_(member_keys or {""})
            )
        ).scalars()
    }

    positions = tuple(
        _evaluate_row(db, rows[key]) for key in sorted(member_keys & set(rows))
    )

    by_dimension: dict[str, dict[str, int]] = {
        dimension.value: {outcome.value: 0 for outcome in ParityOutcome}
        for dimension in ParityDimension
    }
    reasons: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    for position in positions:
        for verdict in position.verdicts:
            by_dimension[verdict.dimension.value][verdict.outcome.value] += 1
            totals[verdict.outcome.value] += 1
            if verdict.reason is not None:
                reasons[verdict.reason.value] += 1

    blockers = tuple(
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
    fingerprint = digest_payload(
        {
            "by_dimension": by_dimension,
            "reasons": dict(sorted(reasons.items())),
            "keys": sorted(position.receivable_key for position in positions),
        }
    )
    return ReceivableParityReport(
        cohort_definition_seal=definition_seal(window),
        evaluated_count=len(positions),
        unprojected_count=len(member_keys - set(rows)),
        matched_count=totals.get(ParityOutcome.MATCHED.value, 0),
        diverged_count=totals.get(ParityOutcome.DIVERGED.value, 0),
        not_expressible_count=totals.get(ParityOutcome.NOT_EXPRESSIBLE.value, 0),
        by_dimension=by_dimension,
        not_expressible_reasons=dict(sorted(reasons.items())),
        positions=positions,
        blockers=blockers,
        report_fingerprint=fingerprint,
    )


__all__ = [
    "DimensionVerdict",
    "PositionParity",
    "ReceivableParityReport",
    "evaluate_receivable_parity",
]
