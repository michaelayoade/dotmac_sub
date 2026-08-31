"""Read-only parity evidence for the composed Collections receivable seam.

Sub's existing invoice and dunning services remain authoritative.  This module
only maps their postpaid invoice facts into the public Collections value object
and compares its pure fail-closed blocker with the incumbent candidate rule.
The aggregate preserves every blocker pair and can repeat the comparison at an
explicit later instant so a currently matched decision cannot hide a
time-dependent divergence.  It never calls the Collections case service,
writes module storage, or supplies a durable module ``source_version``; version
``1`` below is a report-local contract marker and must not be reused by a
stateful reader.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from dotmac_collections import ReceivableObservationV1
from dotmac_kernel.cache import TenantScope
from dotmac_kernel.money import Money, currency
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceLine, InvoiceStatus
from app.services.billing._common import resolve_invoice_settlement_amounts
from app.services.common import round_money, to_decimal
from app.services.invoice_classification import collectible_ar_invoice_filter
from app.services.operator_tenant import OPERATOR_TENANT_ID

_OPEN_STATUSES = {
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
}


class EligibilityParity(StrEnum):
    """Total comparison between incumbent and module eligibility decisions."""

    MATCHED_ACTIONABLE = "matched_actionable"
    MATCHED_BLOCKED = "matched_blocked"
    MODULE_BLOCKED_LEGACY_ACTIONABLE = "module_blocked_legacy_actionable"
    MODULE_ACTIONABLE_LEGACY_BLOCKED = "module_actionable_legacy_blocked"


@dataclass(frozen=True, slots=True)
class PostpaidEligibilityInput:
    """PII-bearing in-memory input; never serialized by the report."""

    invoice_id: UUID
    account_id: UUID
    subscription_ids: tuple[UUID, ...]
    status: InvoiceStatus
    currency_code: str
    receivable: Decimal
    due_at: datetime | None
    due_date_basis: InvoiceDueDateBasis | None
    collectible_ar: bool
    legacy_reconciliation_hold: bool


@dataclass(frozen=True, slots=True)
class EligibilityComparison:
    """One in-memory comparison; identifiers stay out of aggregate output."""

    input: PostpaidEligibilityInput
    legacy_blocker: str | None
    module_blocker: str | None
    parity: EligibilityParity


@dataclass(frozen=True, slots=True)
class BlockerPairCount:
    """Aggregate count for one legacy/module blocker pair."""

    legacy_blocker: str | None
    module_blocker: str | None
    invoices: int

    def as_dict(self) -> dict[str, object]:
        return {
            "legacy_blocker": self.legacy_blocker,
            "module_blocker": self.module_blocker,
            "invoices": self.invoices,
        }


@dataclass(frozen=True, slots=True)
class TemporalParityTransitionCount:
    """Aggregate transition between evaluation and observation decisions."""

    evaluation_parity: EligibilityParity
    observation_parity: EligibilityParity
    invoices: int

    def as_dict(self) -> dict[str, object]:
        return {
            "evaluation_parity": self.evaluation_parity.value,
            "observation_parity": self.observation_parity.value,
            "invoices": self.invoices,
        }


@dataclass(frozen=True, slots=True)
class CollectionsShadowParityReport:
    """Aggregate, PII-free evidence suitable for a review artifact."""

    evaluation_instant: datetime
    observation_instant: datetime
    invoices: int
    matched_actionable: int
    matched_blocked: int
    module_blocked_legacy_actionable: int
    module_actionable_legacy_blocked: int
    null_due_date_basis: int
    explicit_unknown_due_date_basis: int
    subject_scoped_exposures: int
    single_service_exposures: int
    multi_service_exposures: int
    module_blockers: tuple[tuple[str, int], ...]
    blocker_pairs: tuple[BlockerPairCount, ...]
    observation_blocker_pairs: tuple[BlockerPairCount, ...]
    temporal_transitions: tuple[TemporalParityTransitionCount, ...]
    observation_module_blocked_legacy_actionable: int
    observation_module_actionable_legacy_blocked: int
    latent_temporal_mismatches: int

    @property
    def observation_horizon_seconds(self) -> float:
        return (self.observation_instant - self.evaluation_instant).total_seconds()

    @property
    def is_parity_safe(self) -> bool:
        return (
            self.module_blocked_legacy_actionable == 0
            and self.module_actionable_legacy_blocked == 0
            and self.observation_module_blocked_legacy_actionable == 0
            and self.observation_module_actionable_legacy_blocked == 0
        )

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.module_blocked_legacy_actionable:
            reasons.append("module_blocked_legacy_actionable")
        if self.module_actionable_legacy_blocked:
            reasons.append("module_actionable_legacy_blocked")
        if self.latent_temporal_mismatches:
            reasons.append("latent_temporal_mismatch")
        elif (
            self.observation_module_blocked_legacy_actionable
            or self.observation_module_actionable_legacy_blocked
        ):
            reasons.append("observation_mismatch")
        return tuple(reasons)

    def as_dict(self) -> dict[str, object]:
        """Return aggregate evidence only: no ids, amounts, or timestamps."""

        return {
            "observation_horizon_seconds": self.observation_horizon_seconds,
            "invoices": self.invoices,
            "classified": (
                self.matched_actionable
                + self.matched_blocked
                + self.module_blocked_legacy_actionable
                + self.module_actionable_legacy_blocked
            ),
            "matched_actionable": self.matched_actionable,
            "matched_blocked": self.matched_blocked,
            "module_blocked_legacy_actionable": (self.module_blocked_legacy_actionable),
            "module_actionable_legacy_blocked": (self.module_actionable_legacy_blocked),
            "null_due_date_basis": self.null_due_date_basis,
            "explicit_unknown_due_date_basis": self.explicit_unknown_due_date_basis,
            "subject_scoped_exposures": self.subject_scoped_exposures,
            "single_service_exposures": self.single_service_exposures,
            "multi_service_exposures": self.multi_service_exposures,
            "module_blockers": dict(self.module_blockers),
            "blocker_pairs": [item.as_dict() for item in self.blocker_pairs],
            "observation_blocker_pairs": [
                item.as_dict() for item in self.observation_blocker_pairs
            ],
            "temporal_transitions": [
                item.as_dict() for item in self.temporal_transitions
            ],
            "observation_module_blocked_legacy_actionable": (
                self.observation_module_blocked_legacy_actionable
            ),
            "observation_module_actionable_legacy_blocked": (
                self.observation_module_actionable_legacy_blocked
            ),
            "latent_temporal_mismatches": self.latent_temporal_mismatches,
            "blocking_reasons": list(self.blocking_reasons),
            "is_parity_safe": self.is_parity_safe,
        }


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _legacy_blocker(item: PostpaidEligibilityInput, *, as_of: datetime) -> str | None:
    """Mirror ``collections._core._overdue_receivable_snapshot`` admission."""

    if not item.collectible_ar:
        return "not_collectible_ar"
    # Preserve the incumbent's second, raw Python truthiness check. In
    # particular, JSON string ``"false"`` is truthy here even though the SQL
    # classifier's normalized hold predicates admit it.
    if item.legacy_reconciliation_hold:
        return "legacy_reconciliation_hold"
    if item.due_date_basis == InvoiceDueDateBasis.unknown_unverified:
        return "due_date_explicitly_unverified"
    if item.status not in _OPEN_STATUSES:
        return "receivable_not_open"
    if item.receivable <= 0:
        return "no_live_exposure"
    if item.status == InvoiceStatus.overdue:
        return None
    due_at = _aware(item.due_at)
    if due_at is None:
        return "due_date_missing"
    if due_at > as_of:
        return "receivable_not_due"
    return None


def _financial_state(
    status: InvoiceStatus,
) -> Literal["open", "partially_resolved", "resolved", "cancelled"]:
    if status == InvoiceStatus.paid:
        return "resolved"
    if status in {InvoiceStatus.void, InvoiceStatus.written_off}:
        return "cancelled"
    if status == InvoiceStatus.partially_paid:
        return "partially_resolved"
    return "open"


def _module_observation(
    item: PostpaidEligibilityInput, *, as_of: datetime
) -> ReceivableObservationV1:
    due_verified = (
        item.due_at is not None
        and item.due_date_basis is not None
        and item.due_date_basis != InvoiceDueDateBasis.unknown_unverified
    )
    verified_due_at = _aware(item.due_at) if due_verified else None
    amount = (
        item.receivable
        if item.collectible_ar and item.status in _OPEN_STATUSES and item.receivable > 0
        else Decimal(0)
    )
    service_ref = (
        str(item.subscription_ids[0]) if len(item.subscription_ids) == 1 else None
    )
    fingerprint_payload = {
        "collectible_ar": item.collectible_ar,
        "currency": item.currency_code,
        "due_at": (
            verified_due_at.isoformat() if verified_due_at is not None else None
        ),
        "due_date_basis": (
            item.due_date_basis.value if item.due_date_basis is not None else None
        ),
        "invoice_id": str(item.invoice_id),
        "receivable": format(amount, "f"),
        "status": item.status.value,
        "subscriptions": sorted(str(value) for value in item.subscription_ids),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ReceivableObservationV1(
        scope=TenantScope(tenant_id=OPERATOR_TENANT_ID),
        source_owner="financial.invoices",
        exposure_ref=str(item.invoice_id),
        source_version=1,
        state_fingerprint=fingerprint,
        subject_ref=str(item.account_id),
        service_ref=service_ref,
        collection_timing="arrears",
        reason_code="postpaid_overdue",
        collectible_receivable=Money.of(amount, currency(item.currency_code)),
        service_period_status="not_applicable",
        service_period_starts_at=None,
        service_period_ends_at=None,
        due_at=verified_due_at,
        due_date_status="verified" if due_verified else "unknown_unverified",
        financial_state=_financial_state(item.status),
        source_authority="internal",
        projection_mode="authoritative",
        completeness="complete",
        completeness_reason_code=None,
        observed_at=as_of,
    )


def compare_postpaid_eligibility(
    item: PostpaidEligibilityInput, *, as_of: datetime
) -> EligibilityComparison:
    """Compare the incumbent candidate rule with the module's pure blocker."""

    _require_aware("as_of", as_of)
    legacy = _legacy_blocker(item, as_of=as_of)
    module = _module_observation(item, as_of=as_of).automated_collection_blocker(
        as_of=as_of
    )
    if legacy is None and module is None:
        parity = EligibilityParity.MATCHED_ACTIONABLE
    elif legacy is not None and module is not None:
        parity = EligibilityParity.MATCHED_BLOCKED
    elif legacy is None:
        parity = EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE
    else:
        parity = EligibilityParity.MODULE_ACTIONABLE_LEGACY_BLOCKED
    return EligibilityComparison(item, legacy, module, parity)


def _blocker_pair_counts(
    comparisons: tuple[EligibilityComparison, ...],
) -> tuple[BlockerPairCount, ...]:
    counts = Counter((item.legacy_blocker, item.module_blocker) for item in comparisons)
    return tuple(
        BlockerPairCount(
            legacy_blocker=legacy,
            module_blocker=module,
            invoices=count,
        )
        for (legacy, module), count in sorted(
            counts.items(),
            key=lambda item: (
                item[0][0] or "",
                item[0][1] or "",
            ),
        )
    )


def _temporal_transition_counts(
    evaluation: tuple[EligibilityComparison, ...],
    observation: tuple[EligibilityComparison, ...],
) -> tuple[TemporalParityTransitionCount, ...]:
    counts = Counter(
        (at_evaluation.parity, at_observation.parity)
        for at_evaluation, at_observation in zip(evaluation, observation, strict=True)
    )
    return tuple(
        TemporalParityTransitionCount(
            evaluation_parity=at_evaluation,
            observation_parity=at_observation,
            invoices=count,
        )
        for (at_evaluation, at_observation), count in sorted(
            counts.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    )


def _inputs_from_snapshot(db: Session) -> tuple[PostpaidEligibilityInput, ...]:
    invoices = tuple(
        db.scalars(
            select(Invoice)
            .where(Invoice.is_active.is_(True))
            .order_by(Invoice.id.asc())
        ).all()
    )
    collectible_ids = set(
        db.scalars(
            select(Invoice.id)
            .where(Invoice.is_active.is_(True))
            .where(collectible_ar_invoice_filter())
        ).all()
    )
    subscriptions: dict[UUID, set[UUID]] = defaultdict(set)
    for invoice_id, subscription_id in db.execute(
        select(InvoiceLine.invoice_id, InvoiceLine.subscription_id).where(
            InvoiceLine.is_active.is_(True),
            InvoiceLine.subscription_id.is_not(None),
        )
    ):
        subscriptions[invoice_id].add(subscription_id)

    result: list[PostpaidEligibilityInput] = []
    for invoice in invoices:
        settlement = resolve_invoice_settlement_amounts(db, invoice.id)
        # This intentionally mirrors the incumbent dunning snapshot, including
        # its exclusion of opening-funding consumption from postpaid AR.
        receivable = max(
            Decimal("0.00"),
            round_money(
                to_decimal(invoice.total)
                - settlement.payments_applied
                - settlement.credits_applied
            ),
        )
        result.append(
            PostpaidEligibilityInput(
                invoice_id=invoice.id,
                account_id=invoice.account_id,
                subscription_ids=tuple(sorted(subscriptions[invoice.id], key=str)),
                status=invoice.status,
                currency_code=invoice.currency,
                receivable=receivable,
                due_at=_aware(invoice.due_at),
                due_date_basis=invoice.due_date_basis,
                collectible_ar=invoice.id in collectible_ids,
                legacy_reconciliation_hold=bool(
                    (invoice.metadata_ or {}).get("reconciliation_hold")
                ),
            )
        )
    return tuple(result)


def postpaid_eligibility_parity_report(
    db: Session,
    *,
    as_of: datetime | None = None,
    observe_at: datetime | None = None,
) -> CollectionsShadowParityReport:
    """Return blocker-pair and temporal parity from one read-only snapshot."""

    instant = as_of or datetime.now(UTC)
    observation_instant = observe_at or instant
    _require_aware("as_of", instant)
    _require_aware("observe_at", observation_instant)
    if observation_instant < instant:
        raise ValueError("observe_at must not be earlier than as_of")
    inputs = _inputs_from_snapshot(db)
    comparisons = tuple(
        compare_postpaid_eligibility(item, as_of=instant) for item in inputs
    )
    observation_comparisons = tuple(
        compare_postpaid_eligibility(item, as_of=observation_instant) for item in inputs
    )
    parity = Counter(item.parity for item in comparisons)
    observation_parity = Counter(item.parity for item in observation_comparisons)
    blockers = Counter(
        item.module_blocker for item in comparisons if item.module_blocker is not None
    )
    matched_at_evaluation = {
        EligibilityParity.MATCHED_ACTIONABLE,
        EligibilityParity.MATCHED_BLOCKED,
    }
    mismatched_at_observation = {
        EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE,
        EligibilityParity.MODULE_ACTIONABLE_LEGACY_BLOCKED,
    }
    return CollectionsShadowParityReport(
        evaluation_instant=instant,
        observation_instant=observation_instant,
        invoices=len(comparisons),
        matched_actionable=parity[EligibilityParity.MATCHED_ACTIONABLE],
        matched_blocked=parity[EligibilityParity.MATCHED_BLOCKED],
        module_blocked_legacy_actionable=parity[
            EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE
        ],
        module_actionable_legacy_blocked=parity[
            EligibilityParity.MODULE_ACTIONABLE_LEGACY_BLOCKED
        ],
        null_due_date_basis=sum(
            item.input.due_date_basis is None for item in comparisons
        ),
        explicit_unknown_due_date_basis=sum(
            item.input.due_date_basis == InvoiceDueDateBasis.unknown_unverified
            for item in comparisons
        ),
        subject_scoped_exposures=sum(
            len(item.input.subscription_ids) == 0 for item in comparisons
        ),
        single_service_exposures=sum(
            len(item.input.subscription_ids) == 1 for item in comparisons
        ),
        multi_service_exposures=sum(
            len(item.input.subscription_ids) > 1 for item in comparisons
        ),
        module_blockers=tuple(sorted(blockers.items())),
        blocker_pairs=_blocker_pair_counts(comparisons),
        observation_blocker_pairs=_blocker_pair_counts(observation_comparisons),
        temporal_transitions=_temporal_transition_counts(
            comparisons, observation_comparisons
        ),
        observation_module_blocked_legacy_actionable=observation_parity[
            EligibilityParity.MODULE_BLOCKED_LEGACY_ACTIONABLE
        ],
        observation_module_actionable_legacy_blocked=observation_parity[
            EligibilityParity.MODULE_ACTIONABLE_LEGACY_BLOCKED
        ],
        latent_temporal_mismatches=sum(
            at_evaluation.parity in matched_at_evaluation
            and at_observation.parity in mismatched_at_observation
            for at_evaluation, at_observation in zip(
                comparisons, observation_comparisons, strict=True
            )
        ),
    )
