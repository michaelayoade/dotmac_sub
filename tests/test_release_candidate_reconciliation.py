from __future__ import annotations

import pytest

from scripts.release_artifact_contract import (
    GitCommitSha,
    GitTreeSha,
    OCIImageDigest,
    WorkflowRunId,
)
from scripts.release_candidate_reconciliation import (
    CandidateConflictReason,
    CandidateMode,
    CandidateProvenanceObservation,
    ExistingCandidateLabels,
    PriorCandidateEvidence,
    resolve_candidate_provenance,
)

REVISION = GitCommitSha("1" * 40)
OTHER_REVISION = GitCommitSha("2" * 40)
TREE = GitTreeSha("3" * 40)
OTHER_TREE = GitTreeSha("4" * 40)
DIGEST = OCIImageDigest("sha256:" + "5" * 64)
OTHER_DIGEST = OCIImageDigest("sha256:" + "6" * 64)
BUILD_RUN_ID = WorkflowRunId(100)


def _labels(
    *, revision: GitCommitSha = REVISION, source_tree: GitTreeSha = TREE
) -> ExistingCandidateLabels:
    return ExistingCandidateLabels(
        revision=revision, source_tree=source_tree, build_run_id=BUILD_RUN_ID
    )


def _prior(
    *,
    image_digest: OCIImageDigest = DIGEST,
    source_revision: GitCommitSha = REVISION,
    source_tree: GitTreeSha = TREE,
) -> PriorCandidateEvidence:
    return PriorCandidateEvidence(
        image_digest=image_digest,
        source_revision=source_revision,
        source_tree=source_tree,
    )


def test_state_a_neither_tag_nor_evidence_exists_builds() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=False,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.BUILD
    assert decision.accepted
    assert decision.reasons == ()


def test_state_b_tag_and_agreeing_prior_evidence_reuse_without_rebuilding() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=_prior(),
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.REUSE
    assert decision.accepted
    assert decision.resolved_digest == DIGEST
    assert decision.resolved_build_run_id == BUILD_RUN_ID


def test_state_c_registry_exists_but_evidence_expired_still_reuses() -> None:
    """The actual incident: the record is gone (90-day retention), not wrong."""

    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.REUSE
    assert decision.resolved_digest == DIGEST
    assert decision.resolved_build_run_id == BUILD_RUN_ID


def test_state_d_unreadable_labels_fail_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=None,
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert not decision.accepted
    assert decision.reasons == (CandidateConflictReason.LABELS_UNREADABLE,)


def test_state_d_label_revision_mismatch_fails_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(revision=OTHER_REVISION),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.REVISION_MISMATCH,)


def test_state_d_label_tree_mismatch_fails_closed() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(source_tree=OTHER_TREE),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.TREE_MISMATCH,)


def test_state_d_prior_evidence_digest_disagrees_with_registry_fails_closed() -> None:
    """The tag was moved: registry digest and the recorded digest disagree."""

    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(),
        prior_evidence=_prior(image_digest=OTHER_DIGEST),
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.mode is CandidateMode.CONFLICT
    assert decision.reasons == (CandidateConflictReason.PRIOR_EVIDENCE_MISMATCH,)


def test_state_d_both_label_mismatches_are_reported_together() -> None:
    observation = CandidateProvenanceObservation(
        expected_source_revision=REVISION,
        expected_source_tree=TREE,
        tag_present=True,
        tag_digest=DIGEST,
        labels=_labels(revision=OTHER_REVISION, source_tree=OTHER_TREE),
        prior_evidence=None,
    )

    decision = resolve_candidate_provenance(observation)

    assert decision.reasons == (
        CandidateConflictReason.REVISION_MISMATCH,
        CandidateConflictReason.TREE_MISMATCH,
    )


def test_present_tag_without_digest_is_rejected_as_malformed_observation() -> None:
    with pytest.raises(ValueError, match="resolved digest"):
        CandidateProvenanceObservation(
            expected_source_revision=REVISION,
            expected_source_tree=TREE,
            tag_present=True,
            tag_digest=None,
        )


def test_absent_tag_carrying_registry_observations_is_rejected() -> None:
    with pytest.raises(ValueError, match="absent candidate tag"):
        CandidateProvenanceObservation(
            expected_source_revision=REVISION,
            expected_source_tree=TREE,
            tag_present=False,
            tag_digest=DIGEST,
        )
