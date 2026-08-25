"""The `receivable-shadow-01` cohort: which receivable positions are compared.

This module is the *declaration*. It imports no SQLAlchemy, no models and no
session, for the same reason `app.migration_source.cohort` does not: a static
architecture guard has to be able to import the contract directly instead of
re-encoding it in a regex, and a cohort whose definition can only be recovered
by running a query against production is not a sealed cohort.

## Two different things are called a "cohort" in this repository

`app.shadow.cohort` declares a **module-adoption** cohort: twenty-five
independently versioned packages and how far each has actually travelled
(today: `source_only`, `authority_mode = none`, for every one of them).

This module declares a **data** cohort: which *rows* — which receivable
positions across Subscription -> Billing -> Collections — are in scope for the
receivable projection and its semantic parity report.

They are related but neither implies the other:

* the module cohort answers "could `dotmac-billing` one day own this?";
* this cohort answers "which facts would be compared if it ever could".

Recording a data cohort does not move a module one step along
`ADOPTION_PROGRESSION`, and nothing in this package writes to that manifest.
The link runs one way and is deliberate: this module *reads* the module
cohort's pins so that a parity blocker naming `dotmac-subscriptions 0.1.0a2`
cannot drift away from the manifest that actually records that pin.

## The anchor is the incumbent invoice

A receivable position is one row of `invoices` — the incumbent, authoritative
receivable — not one `billing_obligations` row. That direction is the whole
point: the reconciler observes what the incumbent writers already decided. The
ADR 0007 obligation, where one exists for the same subscription and period, is
carried as *provenance* and is the counterparty in the `obligations` parity
dimension, never the source of the projected receivable.

## The membership rule

A candidate is one active `invoices` row. It is a member when, evaluated at a
declared `cutoff_at` over a half-open observation window
`[window_start, window_end)`:

1. `is_active` is true;
2. `status` is in `DECLARED_INVOICE_STATUSES` — `draft` and `void` are out,
   because a draft is not yet a receivable and a void one never was;
3. `issued_at` is non-null and falls inside the observation window;
4. at least one active `invoice_lines` row carries a `subscription_id`;
5. every such line names the *same* subscription;
6. that subscription resolves.

Candidates failing 2-6 are **classified**, never dropped. An excluded row that
is not counted is the difference between a cohort and a convenient sample, and
ADR 0007's cutover-evidence standard requires the classification to be
exhaustive.

## Why the cohort spans both collection modes

`ReceivableLane` has a prepaid member and a postpaid member, and the cohort
admits both. Sub's two delinquency paths diverge operationally —
`financial.dunning` drives the postpaid `DunningWorkflow`, while
`collections.prepaid_balance_sweep` owns a separate scheduled cohort scan with
its own timers, notices and plan — and they converge only at shared financial
access. A cohort that quietly covered postpaid alone would let a
"Subscription -> Billing -> Collections parity" claim rest on half the system.

The lane is read from the resolved subscription's `billing_mode`. It is an
observation carried through the projection, not a routing decision: nothing in
this package selects, triggers, or simulates a dunning path.

## Sealing

Two digests, answering two different questions.

`definition_seal` fingerprints the declaration plus the three window instants.
It is reproducible from this file and the window alone, with no database, so a
reader can verify that two runs claiming the same cohort really applied the
same rule.

`membership_digest` fingerprints the resolved member keys. It needs the
database at the same cutoff. A rebuild that reproduces the `definition_seal`
but not the `membership_digest` has found drift in the *source*, not in the
projection — which is exactly the signal the drift-repair command exists to
surface.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from app.shadow.cohort import SHADOW_COHORT

#: Bumped whenever the membership rule, the declared vocabularies, or the
#: sealed payload change shape. Two runs carrying the same
#: `definition_version` and the same window applied the same rule.
COHORT_DEFINITION_VERSION: Final[str] = "2026-08-25.1"

#: Version of the projection *policy* — the mapping from incumbent rows onto
#: `BillingReceivableProjection` fields. Carried on every projected row so a
#: rebuild can prove it replayed under the same policy rather than merely
#: producing a plausible-looking row.
PROJECTION_POLICY_VERSION: Final[str] = "receivable-projection/1"

COHORT_NAME: Final[str] = "receivable-shadow-01"

#: Evidence schema of the durable run record. Distinct from
#: `COHORT_DEFINITION_VERSION`: the rule and the evidence shape can move
#: independently, and collapsing them would force a fake bump of one to record
#: a real change in the other.
EVIDENCE_SCHEMA_VERSION: Final[int] = 1


class ReceivableLane(StrEnum):
    """The declared collection lane a member position sits in.

    Read from the resolved subscription's `billing_mode`. It is an accounting
    observation carried through the projection, not a routing decision: nothing
    in this package chooses which dunning path would act on the position.
    """

    #: `BillingMode.postpaid` — service delivered, then invoiced. Creates AR.
    POSTPAID_RECEIVABLE = "postpaid_receivable"
    #: `BillingMode.prepaid` — funded service period.
    PREPAID_CONSUMPTION = "prepaid_consumption"


class CohortClassification(StrEnum):
    """Exhaustive disposition of one candidate position.

    Every candidate lands in exactly one of these and the counts sum to the
    candidate total. There is deliberately no residual bucket: a residual
    bucket is where the rows nobody wanted to explain end up.
    """

    #: In the compared set: identity resolved, projection written or planned.
    COVERED = "covered"
    #: Its subscription could not be resolved at all.
    UNRESOLVED = "unresolved"
    #: Lines named more than one subscription where exactly one was required.
    AMBIGUOUS = "ambiguous"
    #: Active in the window, but no active line carries a subscription link.
    UNEXPECTED_UNLINKED = "unexpected_unlinked"
    #: Two candidates collapsed onto one receivable key.
    DUPLICATE = "duplicate"
    #: A declared, expected exclusion — `draft` or `void`.
    EXCLUDED_BY_STATUS = "excluded_by_status"
    #: Resolvable, but every parity dimension is blocked for it.
    NOT_EXPRESSIBLE = "not_expressible"


class ParityDimension(StrEnum):
    """The semantic dimensions the parity report evaluates.

    Each is compared and reported independently. A single aggregate
    "matches / does not match" verdict hides which half of the chain
    disagrees, and the two halves have different owners.
    """

    CADENCE = "cadence"
    PRORATION = "proration"
    OBLIGATIONS = "obligations"
    SETTLEMENTS = "settlements"
    RECEIVABLE_AMOUNT = "receivable_amount"
    DUE_DATE_PROVENANCE = "due_date_provenance"
    SERVICE_SCOPE = "service_scope"


class ParityOutcome(StrEnum):
    """What one dimension concluded for one position."""

    #: Both sides answered and agreed on every compared field.
    MATCHED = "matched"
    #: Both sides answered and disagreed. A real finding.
    DIVERGED = "diverged"
    #: The comparison cannot be made at all. NOT a match, NOT a divergence,
    #: and never folded into either — the count is reported on its own.
    NOT_EXPRESSIBLE = "not_expressible"


class NotExpressibleReason(StrEnum):
    """Closed vocabulary of why a dimension could not be evaluated.

    A closed enum rather than a free-text reason because these counts are
    evidence: "not expressible" that cannot be grouped is indistinguishable
    from "not attempted", and a free string guarantees two spellings of the
    same reason within a release.
    """

    #: Sub's authoritative `subscription_billing_arrangements` record has no
    #: expression in the pinned Subscriptions contract. See
    #: `SUBSCRIPTION_TREATMENT_BLOCKER`.
    SUBSCRIPTION_BILLING_TREATMENT_UNPINNED = "subscription_billing_treatment_unpinned"
    #: No ADR 0007 obligation covers this subscription and period, so there is
    #: no counterparty to compare against. Expected while the ADR 0007 phases
    #: remain pre-cutover; recorded rather than assumed.
    NO_SHADOW_OBLIGATION_IN_WINDOW = "no_shadow_obligation_in_window"
    #: No effective `billing_contract_versions` row at the invoice's issue
    #: instant, so cadence/proration/terms have no declared counterparty.
    NO_EFFECTIVE_CONTRACT_VERSION = "no_effective_contract_version"
    #: The invoice's due-date basis is absent or `unknown_unverified`. Such a
    #: row is a lawful historical observation that cannot drive collection,
    #: and comparing it to a contract-derived expectation would manufacture a
    #: verdict from an unverified input.
    UNVERIFIED_DUE_DATE_PROVENANCE = "unverified_due_date_provenance"
    #: The position mixes currencies. Nominal cross-currency comparison is
    #: forbidden (ADR 0007 invariant 13), so no amount verdict is possible.
    MIXED_CURRENCY_POSITION = "mixed_currency_position"


@dataclass(frozen=True, slots=True)
class ParityBlocker:
    """A named, pinned reason a parity dimension cannot be evaluated.

    A blocker carries the exact coordinates of the pin that causes it. "We
    cannot compare this yet", without naming which pinned artifact would have
    to move, is indistinguishable from "we did not get to it".
    """

    code: str
    dimension: ParityDimension
    reason: NotExpressibleReason
    statement: str
    #: Distribution name of the pinned artifact, e.g. `dotmac-subscriptions`.
    pinned_package: str
    #: The pinned version, read from the module-adoption manifest.
    pinned_version: str
    #: The source revision that version was recorded against.
    pinned_revision: str


def _pinned(module: str) -> tuple[str, str, str]:
    """Read one module's pin coordinates from the module-adoption manifest.

    Read rather than restated. A blocker that names a version maintained in two
    places is a version that will eventually be wrong in one of them.
    """
    for entry in SHADOW_COHORT.modules:
        if entry.module == module:
            return (entry.package, entry.contract_version, entry.source_revision)
    raise KeyError(
        f"{module!r} is not declared in the module-adoption cohort; a parity "
        "blocker cannot name a pin that no manifest records"
    )


_SUBS_PACKAGE, _SUBS_VERSION, _SUBS_REVISION = _pinned("subscriptions")

#: Sub carries its own authoritative non-standard billing treatment
#: (`subscription_billing_arrangements`: complimentary and sponsored). The
#: pinned Subscriptions contract has no counterpart for it — the
#: billing-treatment contract arrived in a later release this repository does
#: not pin.
#:
#: The correct handling is to REFUSE the comparison and count it. Synthesising
#: a local mapping onto a contract Sub does not install would make Sub a second
#: writer of a contract it cannot read, which is the precise failure the
#: source-of-truth standard exists to prevent. So a complimentary or sponsored
#: position is counted `not_expressible` on cadence — never `matched`, and
#: never quietly `diverged` either.
SUBSCRIPTION_TREATMENT_BLOCKER: Final[ParityBlocker] = ParityBlocker(
    code="subscriptions-pin-lacks-billing-treatment-contract",
    dimension=ParityDimension.CADENCE,
    reason=NotExpressibleReason.SUBSCRIPTION_BILLING_TREATMENT_UNPINNED,
    statement=(
        "Cadence and treatment parity cannot be claimed for complimentary or "
        "sponsored subscriptions. Sub's authoritative "
        "subscription_billing_arrangements record has no expression in the "
        "pinned Subscriptions contract, and synthesising one locally would "
        "make Sub a second writer of a contract it does not pin."
    ),
    pinned_package=_SUBS_PACKAGE,
    pinned_version=_SUBS_VERSION,
    pinned_revision=_SUBS_REVISION,
)

#: Blockers that stand for the cohort as a whole, independent of any row.
STANDING_BLOCKERS: Final[tuple[ParityBlocker, ...]] = (SUBSCRIPTION_TREATMENT_BLOCKER,)

#: `InvoiceStatus` members admitted to the cohort. `draft` is not yet a
#: receivable and `void` never was, so both are declared exclusions rather
#: than silent filters. Strings, not the enum, so this module keeps its
#: no-model-import property.
DECLARED_INVOICE_STATUSES: Final[tuple[str, ...]] = (
    "issued",
    "partially_paid",
    "overdue",
    "paid",
    "written_off",
)

#: `InvoiceStatus` members that are declared, expected exclusions.
EXCLUDED_INVOICE_STATUSES: Final[tuple[str, ...]] = ("draft", "void")

#: Treatments carried by `subscription_billing_arrangements` that make the
#: cadence dimension inexpressible against the current pin.
UNPINNED_BILLING_TREATMENTS: Final[tuple[str, ...]] = ("complimentary", "sponsored")

_MODE_LANES: Final[dict[str, ReceivableLane]] = {
    "postpaid": ReceivableLane.POSTPAID_RECEIVABLE,
    "prepaid": ReceivableLane.PREPAID_CONSUMPTION,
}


def lane_for_billing_mode(billing_mode: str) -> ReceivableLane | None:
    """Map a subscription's declared billing mode onto its lane, or `None`.

    `None` means the mode is outside the declared vocabulary. The caller must
    classify such a candidate as `UNRESOLVED` rather than guess a lane: an
    invented lane would put the position in a collection mode nobody chose.
    """
    return _MODE_LANES.get(billing_mode)


class CohortWindowError(ValueError):
    """The declared observation window cannot define a reproducible cohort."""


@dataclass(frozen=True, slots=True)
class ReceivableCohortWindow:
    """The three instants that make a cohort reproducible.

    Naive datetimes are refused rather than coerced. A cohort whose boundary
    depends on the reader's local timezone is not sealed, and stamping UTC on
    an ambiguous value would hide exactly that.
    """

    cutoff_at: datetime
    window_start: datetime
    window_end: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("cutoff_at", self.cutoff_at),
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CohortWindowError(
                    f"{label} must be timezone-aware; a naive instant cannot "
                    "seal a reproducible cohort"
                )
        if self.window_end <= self.window_start:
            raise CohortWindowError(
                "window_end must be strictly after window_start; the "
                "observation window is half-open [start, end)"
            )
        if self.cutoff_at < self.window_end:
            raise CohortWindowError(
                "cutoff_at must not precede window_end; a cohort cannot be "
                "sealed at an instant before the facts it claims to observe"
            )

    def as_utc(self) -> ReceivableCohortWindow:
        """The same window with every instant normalised to UTC."""
        return ReceivableCohortWindow(
            cutoff_at=self.cutoff_at.astimezone(UTC),
            window_start=self.window_start.astimezone(UTC),
            window_end=self.window_end.astimezone(UTC),
        )

    def contains(self, moment: datetime) -> bool:
        """True when `moment` falls in the half-open observation window."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise CohortWindowError(
                "cohort membership cannot be decided for a naive instant"
            )
        window = self.as_utc()
        instant = moment.astimezone(UTC)
        return window.window_start <= instant < window.window_end


def digest_payload(payload: object) -> str:
    """Deterministic sha256 over a JSON-serialisable payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def definition_payload(window: ReceivableCohortWindow) -> dict[str, object]:
    """The exact payload `definition_seal` fingerprints.

    Exposed rather than private so a test can assert on the payload instead of
    on an opaque hex string. A seal test that only compares two digests passes
    just as happily when both are computed from nothing.
    """
    utc = window.as_utc()
    return {
        "cohort_name": COHORT_NAME,
        "definition_version": COHORT_DEFINITION_VERSION,
        "projection_policy_version": PROJECTION_POLICY_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "cutoff_at": utc.cutoff_at.isoformat(),
        "window_start": utc.window_start.isoformat(),
        "window_end": utc.window_end.isoformat(),
        "anchor": "invoices",
        "declared_invoice_statuses": sorted(DECLARED_INVOICE_STATUSES),
        "excluded_invoice_statuses": sorted(EXCLUDED_INVOICE_STATUSES),
        "unpinned_billing_treatments": sorted(UNPINNED_BILLING_TREATMENTS),
        "lanes": sorted(item.value for item in ReceivableLane),
        "classifications": sorted(item.value for item in CohortClassification),
        "parity_dimensions": sorted(item.value for item in ParityDimension),
        "parity_outcomes": sorted(item.value for item in ParityOutcome),
        "not_expressible_reasons": sorted(item.value for item in NotExpressibleReason),
        "standing_blockers": sorted(
            (
                blocker.code,
                blocker.dimension.value,
                blocker.reason.value,
                blocker.pinned_package,
                blocker.pinned_version,
                blocker.pinned_revision,
            )
            for blocker in STANDING_BLOCKERS
        ),
    }


def definition_seal(window: ReceivableCohortWindow) -> str:
    """Fingerprint the cohort *rule*. No database required."""
    return digest_payload(definition_payload(window))


def membership_digest(receivable_keys: Iterable[str]) -> str:
    """Fingerprint the resolved member set.

    Sorted and de-duplicated: the digest must depend on which positions are in
    the cohort, never on the order a query happened to return them, and never
    on a duplicate the classifier has already counted separately.
    """
    return digest_payload(sorted(set(receivable_keys)))


def receivable_key(*, invoice_id: str, lane: ReceivableLane) -> str:
    """The stable natural key of one receivable position.

    The invoice is the anchor because it is the incumbent authoritative
    receivable. The lane is carried in the key on purpose: a subscription that
    moves between prepaid and postpaid produces a *new* position rather than
    silently rewriting the accounting meaning of an existing projected row.
    """
    return f"{lane.value}:{invoice_id}"


__all__ = [
    "COHORT_DEFINITION_VERSION",
    "COHORT_NAME",
    "DECLARED_INVOICE_STATUSES",
    "EVIDENCE_SCHEMA_VERSION",
    "EXCLUDED_INVOICE_STATUSES",
    "PROJECTION_POLICY_VERSION",
    "STANDING_BLOCKERS",
    "SUBSCRIPTION_TREATMENT_BLOCKER",
    "UNPINNED_BILLING_TREATMENTS",
    "CohortClassification",
    "CohortWindowError",
    "NotExpressibleReason",
    "ParityBlocker",
    "ParityDimension",
    "ParityOutcome",
    "ReceivableCohortWindow",
    "ReceivableLane",
    "definition_payload",
    "definition_seal",
    "digest_payload",
    "lane_for_billing_mode",
    "membership_digest",
    "receivable_key",
]
