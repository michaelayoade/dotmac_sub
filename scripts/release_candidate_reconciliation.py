"""Reconcile a `candidate-<sha>` tag that may already exist in the registry.

`release-candidate.yml`'s "Refuse duplicate candidate build" step used to be a
pure existence check on the mutable `candidate-<sha>` tag: if the tag was
there, the workflow refused to run again for that commit, forever. That was
correct for the case it was written for (someone re-dispatching a build that
already succeeded) and wrong for the case that actually happened in
production: the build succeeded, pushed the image, and then the immediately
following digest-visibility check failed on a transient GHCR propagation
delay. The tag existed; the run still failed; nothing could ever rerun it
except a human deleting the GHCR package version by hand.

This module is the pure decision the workflow now makes instead. Every field
of a re-derived candidate evidence document -- except which run wrote it -- is
recoverable from the immutable image itself, so a resume needs zero state from
the failed run. Prior evidence, when it can still be found, is used only as a
cross-check constraint, never as the source of the new document.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scripts.release_artifact_contract import (
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    ReleaseContractError,
    WorkflowRunId,
)


class CandidateMode(str, Enum):
    """The resolved action for one `candidate-<sha>` tag."""

    BUILD = "build"
    REUSE = "reuse"
    CONFLICT = "conflict"


class CandidateConflictReason(str, Enum):
    """Stable reasons an existing candidate tag cannot be safely reused."""

    LABELS_UNREADABLE = "labels_unreadable"
    REVISION_MISMATCH = "revision_mismatch"
    TREE_MISMATCH = "tree_mismatch"
    PRIOR_EVIDENCE_MISMATCH = "prior_evidence_mismatch"


@dataclass(frozen=True, slots=True)
class ExistingCandidateLabels:
    """Provenance labels read directly off an already-published candidate image."""

    revision: GitCommitSha
    source_tree: GitTreeSha
    build_run_id: WorkflowRunId


@dataclass(frozen=True, slots=True)
class PriorCandidateEvidence:
    """A previously uploaded `candidate.json` found for this same commit.

    Absence is never proof the candidate was never verified -- the evidence
    artifact carries a 90-day retention window, so it can simply have expired.
    Presence is used only as an extra cross-check against the registry.
    """

    image_digest: OCIImageDigest
    source_revision: GitCommitSha
    source_tree: GitTreeSha


@dataclass(frozen=True, slots=True)
class CandidateProvenanceObservation:
    """Everything independently observable about one candidate tag right now."""

    expected_source_revision: GitCommitSha
    expected_source_tree: GitTreeSha
    tag_present: bool
    tag_digest: OCIImageDigest | None = None
    labels: ExistingCandidateLabels | None = None
    prior_evidence: PriorCandidateEvidence | None = None

    def __post_init__(self) -> None:
        if self.tag_present and self.tag_digest is None:
            raise ValueError("a present candidate tag requires its resolved digest")
        if not self.tag_present and (
            self.tag_digest is not None
            or self.labels is not None
            or self.prior_evidence is not None
        ):
            raise ValueError(
                "an absent candidate tag carries no further registry observations"
            )


@dataclass(frozen=True, slots=True)
class CandidateReconciliationDecision:
    """The resolved mode plus, on reuse, what to reuse."""

    mode: CandidateMode
    reasons: tuple[CandidateConflictReason, ...] = ()
    resolved_digest: OCIImageDigest | None = None
    resolved_build_run_id: WorkflowRunId | None = None

    @property
    def accepted(self) -> bool:
        return self.mode is not CandidateMode.CONFLICT


def resolve_candidate_provenance(
    observation: CandidateProvenanceObservation,
) -> CandidateReconciliationDecision:
    """Resolve one candidate tag into build / reuse / conflict.

    - Neither tag nor prior evidence exists: BUILD (state A, unchanged).
    - The tag exists, its labels agree with this commit, and any prior
      evidence found agrees with the registry: REUSE without rebuilding --
      whether or not prior evidence was found (states B and C are the same
      decision; C is simply B with expired/missing evidence).
    - Labels are unreadable, disagree with this commit, or prior evidence
      disagrees with the registry: CONFLICT (state D), fail closed.
    """

    if not observation.tag_present:
        return CandidateReconciliationDecision(mode=CandidateMode.BUILD)

    if observation.labels is None:
        return CandidateReconciliationDecision(
            mode=CandidateMode.CONFLICT,
            reasons=(CandidateConflictReason.LABELS_UNREADABLE,),
        )

    reasons: list[CandidateConflictReason] = []
    labels = observation.labels
    if labels.revision != observation.expected_source_revision:
        reasons.append(CandidateConflictReason.REVISION_MISMATCH)
    if labels.source_tree != observation.expected_source_tree:
        reasons.append(CandidateConflictReason.TREE_MISMATCH)

    prior = observation.prior_evidence
    if prior is not None and (
        prior.image_digest != observation.tag_digest
        or prior.source_revision != observation.expected_source_revision
        or prior.source_tree != observation.expected_source_tree
    ):
        reasons.append(CandidateConflictReason.PRIOR_EVIDENCE_MISMATCH)

    if reasons:
        return CandidateReconciliationDecision(
            mode=CandidateMode.CONFLICT,
            reasons=tuple(reasons),
        )

    return CandidateReconciliationDecision(
        mode=CandidateMode.REUSE,
        resolved_digest=observation.tag_digest,
        resolved_build_run_id=labels.build_run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("--expected-source-revision", required=True)
    resolve.add_argument("--expected-source-tree", required=True)
    resolve.add_argument("--tag-present", action="store_true")
    resolve.add_argument("--tag-digest")
    resolve.add_argument("--label-revision")
    resolve.add_argument("--label-tree")
    resolve.add_argument("--label-build-run-id", type=int)
    resolve.add_argument("--prior-evidence-digest")
    resolve.add_argument("--prior-evidence-revision")
    resolve.add_argument("--prior-evidence-tree")
    resolve.add_argument("--github-output", required=True, type=Path)
    return parser


def _append_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    labels: ExistingCandidateLabels | None = None
    if args.label_revision and args.label_tree and args.label_build_run_id:
        labels = ExistingCandidateLabels(
            revision=GitCommitSha(args.label_revision),
            source_tree=GitTreeSha(args.label_tree),
            build_run_id=WorkflowRunId(args.label_build_run_id),
        )

    prior_evidence: PriorCandidateEvidence | None = None
    if (
        args.prior_evidence_digest
        and args.prior_evidence_revision
        and args.prior_evidence_tree
    ):
        prior_evidence = PriorCandidateEvidence(
            image_digest=OCIImageDigest(args.prior_evidence_digest),
            source_revision=GitCommitSha(args.prior_evidence_revision),
            source_tree=GitTreeSha(args.prior_evidence_tree),
        )

    try:
        observation = CandidateProvenanceObservation(
            expected_source_revision=GitCommitSha(args.expected_source_revision),
            expected_source_tree=GitTreeSha(args.expected_source_tree),
            tag_present=args.tag_present,
            tag_digest=(OCIImageDigest(args.tag_digest) if args.tag_digest else None),
            labels=labels,
            prior_evidence=prior_evidence,
        )
    except (ReleaseContractError, ValueError) as exc:
        print(f"candidate provenance observation is invalid: {exc}")
        return 1

    decision = resolve_candidate_provenance(observation)
    if not decision.accepted:
        print(
            "candidate provenance conflict: "
            + ", ".join(reason.value for reason in decision.reasons)
        )
        return 1

    outputs = {"mode": decision.mode.value}
    if decision.resolved_digest is not None:
        outputs["resolved_digest"] = decision.resolved_digest.value
    if decision.resolved_build_run_id is not None:
        outputs["resolved_build_run_id"] = str(decision.resolved_build_run_id.value)
    _append_github_outputs(args.github_output, outputs)
    print(decision.mode.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
