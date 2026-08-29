"""Fail-closed policy for production post-migration deploy resume."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scripts.release_artifact_contract import (
    AlembicHeads,
    OCIImageDigest,
    ReleaseContractError,
    WorkflowRunId,
)

RESUME_MODE = "skip_production_post_migration_resume"


class ProductionResumeIssue(str, Enum):
    """Stable reasons a production resume cannot skip backup and migration."""

    AUTHORIZATION_RUN_MISMATCH = "authorization_run_mismatch"
    CANDIDATE_DIGEST_MISMATCH = "candidate_digest_mismatch"
    BACKUP_ARTIFACT_MISSING = "backup_artifact_missing"
    BACKUP_ARTIFACT_NOT_FROM_FAILED_RUN = "backup_artifact_not_from_failed_run"
    DATABASE_NOT_AT_CANDIDATE_HEADS = "database_not_at_candidate_heads"
    CURRENT_IMAGE_NOT_ROLLBACK_BOUNDARY = "current_image_not_rollback_boundary"


@dataclass(frozen=True, slots=True)
class ProductionPostMigrationResumeEvidence:
    """Evidence required to resume after backup and migration already completed."""

    prior_failed_run_id: WorkflowRunId
    authorization_run_id: WorkflowRunId
    expected_authorization_run_id: WorkflowRunId
    backup_path_exists: bool
    backup_path_names_failed_run: bool
    candidate_digest: OCIImageDigest
    authorized_digest: OCIImageDigest
    database_heads: AlembicHeads
    candidate_heads: AlembicHeads
    current_app_image: str
    previous_image: str
    candidate_image: str

    def __post_init__(self) -> None:
        for field in ("current_app_image", "previous_image", "candidate_image"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} is required")


@dataclass(frozen=True, slots=True)
class ProductionPostMigrationResumeDecision:
    """Resolved resume behavior."""

    mode: str
    issues: tuple[ProductionResumeIssue, ...]

    @property
    def accepted(self) -> bool:
        return not self.issues


def resolve_post_migration_resume(
    evidence: ProductionPostMigrationResumeEvidence,
) -> ProductionPostMigrationResumeDecision:
    """Accept only the exact state left by a failed post-migration rollout."""

    issues: list[ProductionResumeIssue] = []
    if evidence.authorization_run_id != evidence.expected_authorization_run_id:
        issues.append(ProductionResumeIssue.AUTHORIZATION_RUN_MISMATCH)
    if evidence.candidate_digest != evidence.authorized_digest:
        issues.append(ProductionResumeIssue.CANDIDATE_DIGEST_MISMATCH)
    if not evidence.backup_path_exists:
        issues.append(ProductionResumeIssue.BACKUP_ARTIFACT_MISSING)
    if not evidence.backup_path_names_failed_run:
        issues.append(ProductionResumeIssue.BACKUP_ARTIFACT_NOT_FROM_FAILED_RUN)
    if evidence.database_heads != evidence.candidate_heads:
        issues.append(ProductionResumeIssue.DATABASE_NOT_AT_CANDIDATE_HEADS)
    if evidence.current_app_image not in {
        evidence.previous_image,
        evidence.candidate_image,
    }:
        issues.append(ProductionResumeIssue.CURRENT_IMAGE_NOT_ROLLBACK_BOUNDARY)
    return ProductionPostMigrationResumeDecision(
        mode=RESUME_MODE if not issues else "required",
        issues=tuple(issues),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-post-migration")
    verify.add_argument("--prior-failed-run-id", required=True, type=int)
    verify.add_argument("--authorization-run-id", required=True, type=int)
    verify.add_argument("--expected-authorization-run-id", required=True, type=int)
    verify.add_argument("--backup-path", required=True, type=Path)
    verify.add_argument("--candidate-digest", required=True)
    verify.add_argument("--authorized-digest", required=True)
    verify.add_argument("--database-head", action="append", required=True)
    verify.add_argument("--candidate-head", action="append", required=True)
    verify.add_argument("--current-app-image", required=True)
    verify.add_argument("--previous-image", required=True)
    verify.add_argument("--candidate-image", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    backup_path = args.backup_path
    try:
        evidence = ProductionPostMigrationResumeEvidence(
            prior_failed_run_id=WorkflowRunId(args.prior_failed_run_id),
            authorization_run_id=WorkflowRunId(args.authorization_run_id),
            expected_authorization_run_id=WorkflowRunId(
                args.expected_authorization_run_id
            ),
            backup_path_exists=backup_path.is_file() and backup_path.stat().st_size > 0,
            backup_path_names_failed_run=str(args.prior_failed_run_id)
            in backup_path.name,
            candidate_digest=OCIImageDigest(args.candidate_digest),
            authorized_digest=OCIImageDigest(args.authorized_digest),
            database_heads=AlembicHeads(tuple(args.database_head)),
            candidate_heads=AlembicHeads(tuple(args.candidate_head)),
            current_app_image=args.current_app_image,
            previous_image=args.previous_image,
            candidate_image=args.candidate_image,
        )
    except (OSError, ReleaseContractError, ValueError) as exc:
        print(f"production resume evidence is invalid: {exc}")
        return 1
    decision = resolve_post_migration_resume(evidence)
    if not decision.accepted:
        print(
            "production post-migration resume rejected: "
            + ", ".join(issue.value for issue in decision.issues)
        )
        return 1
    print(decision.mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
