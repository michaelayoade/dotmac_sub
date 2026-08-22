"""Sub's binding to the accepted Governance ISP-replacement programme.

Sub is `asm-dotmac-sub-legacy`, the **source-authoritative** assembly of
`pgm-dotmac-isp-replacement`. This module records which immutable Governance
revision that claim comes from, so nothing downstream has to re-derive the
cohort, invent its membership, or reorder it.

## Why the binding is a value object rather than a comment

The cohort, its ordering and its controls live in one controlled record —
`programmes/dotmac-isp-replacement.json` in `dotmac_governance`. Governance
owns programme identity; Sub owns none of it. A prose reference to "cohort 1"
in a docstring drifts silently the moment Governance renumbers or renames.
A parsed, validated binding pinned to a 40-character commit cannot: the
revision either names bytes that exist or it does not.

The revision is deliberately a commit, never a tag or branch. ADR 0012's
drift-prevention clause requires exactly that, and for the usual reason — a
tag is a pointer its publisher can move after the plan naming it was approved.

## What this module may not become

It may not become a second programme record. Nothing here approves a control,
opens a cohort, names a deployment host or moves authority. `CohortState` has
no `open` member and `SourceReadinessClaim` has no member that spells adoption
or cutover, so neither can be recorded here even by mistake — a structural
property, not a convention a later edit could quietly drop.

The readiness work this repository performs produces *inputs* to `ctl-isp-006`
and `ctl-isp-009`. Producing an input is not verifying a control: a `verified`
control requires an immutable controlled-source reference that only Governance
can record.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

#: The controlled repository that owns programme identity. Recorded so a
#: reader can find the record; Sub never writes to it.
GOVERNANCE_REPOSITORY: Final[str] = "https://github.com/michaelayoade/dotmac_governance"

#: `feat(programme): record resolved decisions and answer dec-isp-003 through
#: dec-isp-007 (#25)`. Pinned as a literal because the whole point of this file
#: is that the pin cannot move without a reviewed diff.
#:
#: Repinned 2026-08-22 from `68c7a62e…`, which accepted the programme. That
#: revision predates five answered decisions and a sixth cohort-1 component, so
#: a binding still pointing at it described a cohort that no longer exists.
ACCEPTED_REVISION: Final[str] = "d91a87f6823bfd2afa6c2025bdb1af644331fa39"

PROGRAMME_ID: Final[str] = "pgm-dotmac-isp-replacement"
PROGRAMME_RECORD_PATH: Final[str] = "programmes/dotmac-isp-replacement.json"
GOVERNING_DECISION_PATH: Final[str] = (
    "docs/adr/0012-dotmac-isp-replacement-programme.md"
)

SOURCE_ASSEMBLY_ID: Final[str] = "asm-dotmac-sub-legacy"
TARGET_ASSEMBLY_ID: Final[str] = "asm-dotmac-isp"

#: The track this repository's readiness work belongs to. The other track,
#: `track-isp-target-build`, is not Sub's and is named here only so a reader
#: does not mistake one for the other.
SOURCE_TRACK_ID: Final[str] = "track-isp-sub-cutover"

_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")


class GovernanceBindingError(ValueError):
    """This value cannot bind Sub to the accepted programme."""


class CohortState(StrEnum):
    """How far a Governance cohort has actually been taken.

    There is deliberately no `open`, `active` or `cut-over` member. Every
    cohort in the accepted matrix is `blocked` behind the full cutover-control
    set, and a vocabulary that cannot spell "open" cannot be edited into
    claiming a cohort was opened from inside the source repository.
    """

    #: Named and ordered by Governance; every cutover control still unmet.
    BLOCKED = "blocked"


class SourceReadinessClaim(StrEnum):
    """What source-readiness work is entitled to claim about itself.

    Note what is absent: composed, adopted, backfilled, shadowed, cut over,
    retired. Those are target-track or post-cutover states, and none of them
    can be reached by characterising a source. Keeping them out of the
    vocabulary is what stops "we built the export" from being read later as
    "the data moved".
    """

    #: Surfaces are enumerated and classified; nothing is exported yet.
    INVENTORIED = "inventoried"
    #: A typed, versioned read-only export contract exists and is tested.
    EXPORT_CONTRACT_READY = "export_contract_ready"
    #: Deterministic comparison digests exist and are tested.
    DIGEST_CONTRACT_READY = "digest_contract_ready"
    #: The current writer surface is frozen by a two-directional ratchet.
    WRITER_SURFACE_RATCHETED = "writer_surface_ratcheted"


class CutoverControl(BaseModel):
    """One Governance cutover control, as Sub is entitled to describe it.

    `state` mirrors the accepted record. Sub never advances it: a control moves
    only in Governance, citing an immutable controlled-source reference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    control_id: str
    name: str
    #: Verbatim from the accepted matrix at `ACCEPTED_REVISION`.
    state: str
    #: True when Sub's source-readiness work produces evidence this control
    #: will eventually cite. Producing evidence is not verifying the control.
    sub_supplies_evidence: bool


class GovernanceBinding(BaseModel):
    """Sub's read-only view of the accepted programme, valid by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    revision: str
    programme_id: str
    record_path: str
    decision_path: str
    source_assembly_id: str
    target_assembly_id: str
    track_id: str
    cohort_id: str
    cohort_sequence: int
    cohort_name: str
    cohort_state: CohortState
    controls: tuple[CutoverControl, ...]
    #: Governance `open_decisions` that still gate this cohort. Recorded so the
    #: readiness report names the real blocker instead of implying none exists.
    unresolved_decision_ids: tuple[str, ...]
    #: Governance `resolved_decisions` bearing on this cohort. Sub's view could
    #: previously only say what was still open, so an answered decision left no
    #: trace here and the binding read as though nothing had been settled.
    resolved_decision_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> GovernanceBinding:
        if _COMMIT_RE.fullmatch(self.revision) is None:
            raise GovernanceBindingError(
                f"{self.revision!r} is not a 40-character lowercase commit. A "
                "programme binding must pin bytes, not a tag or branch a "
                "publisher can move after the decision citing it was accepted."
            )
        if self.cohort_sequence < 1:
            raise GovernanceBindingError(
                "cohort sequence is 1-based in the accepted matrix; "
                f"got {self.cohort_sequence}"
            )
        if not self.controls:
            raise GovernanceBindingError(
                "a cohort with no cutover controls would read as a cohort with "
                "nothing left to prove"
            )
        if not any(control.sub_supplies_evidence for control in self.controls):
            raise GovernanceBindingError(
                "no control names Sub as an evidence producer, so this binding "
                "cannot explain why source-readiness work exists at all"
            )
        both = set(self.unresolved_decision_ids) & set(self.resolved_decision_ids)
        if both:
            raise GovernanceBindingError(
                "a decision is open or answered, never both; " + ", ".join(sorted(both))
            )
        return self

    @property
    def evidence_control_ids(self) -> tuple[str, ...]:
        """Controls this repository's readiness work produces inputs for."""

        return tuple(
            control.control_id
            for control in self.controls
            if control.sub_supplies_evidence
        )


#: The accepted first cohort, transcribed from the record at
#: `ACCEPTED_REVISION`. Its membership is Governance's, not Sub's: the six
#: components are kernel and UI (reuse), `dotmac-party` (release),
#: `dotmac-brand-profiles` (adopt), `dotmac-customers` (build) and
#: `dotmac-addresses` (build).
#:
#: `dotmac-addresses` joined on 2026-08-21 by dec-isp-007, which gave customer
#: addresses a named owner for the first time — they had none on either side,
#: which is why `addresses` is the one cohort-1 table Sub still cannot route
#: through an owner.
COHORT_ID: Final[str] = "cohort-isp-01"
COHORT_SEQUENCE: Final[int] = 1
COHORT_NAME: Final[str] = "Foundation party and customer"

BINDING: Final[GovernanceBinding] = GovernanceBinding(
    repository=GOVERNANCE_REPOSITORY,
    revision=ACCEPTED_REVISION,
    programme_id=PROGRAMME_ID,
    record_path=PROGRAMME_RECORD_PATH,
    decision_path=GOVERNING_DECISION_PATH,
    source_assembly_id=SOURCE_ASSEMBLY_ID,
    target_assembly_id=TARGET_ASSEMBLY_ID,
    track_id=SOURCE_TRACK_ID,
    cohort_id=COHORT_ID,
    cohort_sequence=COHORT_SEQUENCE,
    cohort_name=COHORT_NAME,
    cohort_state=CohortState.BLOCKED,
    controls=(
        CutoverControl(
            control_id="ctl-isp-001",
            name=(
                "Coordinated target-build and Sub-cutover programme receives "
                "attributable human approval"
            ),
            state="verified",
            sub_supplies_evidence=False,
        ),
        CutoverControl(
            control_id="ctl-isp-002",
            name=(
                "Target assembly repository runtime database and production "
                "deployment owner are named"
            ),
            state="blocked",
            sub_supplies_evidence=False,
        ),
        CutoverControl(
            control_id="ctl-isp-003",
            name=(
                "Legacy Sub transition rule permits only bounded cutover work "
                "and justified local-writer retirement"
            ),
            state="blocked",
            sub_supplies_evidence=False,
        ),
        CutoverControl(
            control_id="ctl-isp-006",
            name=(
                "Every source row has an accepted quarantine or "
                "evidenced-retirement disposition and replay is idempotent"
            ),
            state="blocked",
            sub_supplies_evidence=True,
        ),
        CutoverControl(
            control_id="ctl-isp-007",
            name=(
                "Complete cohort shadow reaches zero unexplained drift at an "
                "immutable source watermark"
            ),
            state="blocked",
            sub_supplies_evidence=True,
        ),
        CutoverControl(
            control_id="ctl-isp-009",
            name=(
                "Displaced writers and fallbacks reach a bidirectional ratchet "
                "of zero before rollback closure"
            ),
            state="blocked",
            sub_supplies_evidence=True,
        ),
    ),
    unresolved_decision_ids=("dec-isp-002",),
    resolved_decision_ids=(
        "dec-isp-003",
        "dec-isp-004",
        "dec-isp-005",
        "dec-isp-006",
        "dec-isp-007",
    ),
)

#: What this repository's readiness slice is entitled to say it achieved.
#: Read it next to `BINDING.cohort_state`: the claims are real, and the cohort
#: is still blocked. Both statements are true at once, and the pairing is the
#: point.
CLAIMS: Final[tuple[SourceReadinessClaim, ...]] = (
    SourceReadinessClaim.INVENTORIED,
    SourceReadinessClaim.EXPORT_CONTRACT_READY,
    SourceReadinessClaim.DIGEST_CONTRACT_READY,
    SourceReadinessClaim.WRITER_SURFACE_RATCHETED,
)


__all__ = [
    "ACCEPTED_REVISION",
    "BINDING",
    "CLAIMS",
    "COHORT_ID",
    "COHORT_NAME",
    "COHORT_SEQUENCE",
    "CohortState",
    "CutoverControl",
    "GOVERNANCE_REPOSITORY",
    "GOVERNING_DECISION_PATH",
    "GovernanceBinding",
    "GovernanceBindingError",
    "PROGRAMME_ID",
    "PROGRAMME_RECORD_PATH",
    "SOURCE_ASSEMBLY_ID",
    "SOURCE_TRACK_ID",
    "SourceReadinessClaim",
    "TARGET_ASSEMBLY_ID",
]
