from __future__ import annotations

from scripts.deploy_resume_policy import (
    RESUME_MODE,
    ProductionPostMigrationResumeEvidence,
    ProductionResumeIssue,
    resolve_post_migration_resume,
)
from scripts.release_artifact_contract import (
    AlembicHeads,
    OCIImageDigest,
    WorkflowRunId,
)

DIGEST = OCIImageDigest("sha256:" + "1" * 64)


def _evidence(
    *,
    authorization_run_id: WorkflowRunId = WorkflowRunId(20),
    expected_authorization_run_id: WorkflowRunId = WorkflowRunId(20),
    backup_path_exists: bool = True,
    backup_path_names_failed_run: bool = True,
    authorized_digest: OCIImageDigest = DIGEST,
    database_heads: AlembicHeads = AlembicHeads(("557_outbox_relay_prereq",)),
    candidate_heads: AlembicHeads = AlembicHeads(("557_outbox_relay_prereq",)),
    current_app_image: str = "ghcr.io/michaelayoade/dotmac_sub@sha256:" + "0" * 64,
) -> ProductionPostMigrationResumeEvidence:
    return ProductionPostMigrationResumeEvidence(
        prior_failed_run_id=WorkflowRunId(10),
        authorization_run_id=authorization_run_id,
        expected_authorization_run_id=expected_authorization_run_id,
        backup_path_exists=backup_path_exists,
        backup_path_names_failed_run=backup_path_names_failed_run,
        candidate_digest=DIGEST,
        authorized_digest=authorized_digest,
        database_heads=database_heads,
        candidate_heads=candidate_heads,
        current_app_image=current_app_image,
        previous_image="ghcr.io/michaelayoade/dotmac_sub@sha256:" + "0" * 64,
        candidate_image="ghcr.io/michaelayoade/dotmac_sub@sha256:" + "1" * 64,
    )


def test_post_migration_resume_accepts_exact_candidate_evidence() -> None:
    decision = resolve_post_migration_resume(_evidence())

    assert decision.accepted
    assert decision.mode == RESUME_MODE
    assert decision.issues == ()


def test_post_migration_resume_refuses_mismatched_database_heads() -> None:
    decision = resolve_post_migration_resume(
        _evidence(database_heads=AlembicHeads(("556_idempotency_ledger_prereq",)))
    )

    assert not decision.accepted
    assert decision.issues == (ProductionResumeIssue.DATABASE_NOT_AT_CANDIDATE_HEADS,)


def test_post_migration_resume_refuses_changed_digest_and_auth_run() -> None:
    decision = resolve_post_migration_resume(
        _evidence(
            authorization_run_id=WorkflowRunId(21),
            authorized_digest=OCIImageDigest("sha256:" + "2" * 64),
        )
    )

    assert decision.issues == (
        ProductionResumeIssue.AUTHORIZATION_RUN_MISMATCH,
        ProductionResumeIssue.CANDIDATE_DIGEST_MISMATCH,
    )


def test_post_migration_resume_refuses_missing_prior_backup_evidence() -> None:
    decision = resolve_post_migration_resume(
        _evidence(backup_path_exists=False, backup_path_names_failed_run=False)
    )

    assert decision.issues == (
        ProductionResumeIssue.BACKUP_ARTIFACT_MISSING,
        ProductionResumeIssue.BACKUP_ARTIFACT_NOT_FROM_FAILED_RUN,
    )


def test_post_migration_resume_requires_current_image_at_rollback_boundary() -> None:
    decision = resolve_post_migration_resume(
        _evidence(
            current_app_image="ghcr.io/michaelayoade/dotmac_sub@sha256:" + "9" * 64
        )
    )

    assert decision.issues == (
        ProductionResumeIssue.CURRENT_IMAGE_NOT_ROLLBACK_BOUNDARY,
    )
