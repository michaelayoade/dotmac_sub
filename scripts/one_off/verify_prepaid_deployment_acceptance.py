"""Fail-closed acceptance check for the prepaid renewal deployment.

This command composes canonical owners and performs no writes. It verifies the
running revision, database head, materialized funding authority, readiness and
enforcement controls, then fingerprints the complete exact-evidence service
coverage cohort. PostgreSQL sessions are read-only, primary execution requires
``--allow-primary``, and the session is always rolled back.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.prepaid_enforcement import PrepaidEnforcementReadiness
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.services import control_registry
from app.services.prepaid_coverage_reconciliation import (
    preview_prepaid_coverage_reconciliation,
)
from app.services.prepaid_enforcement_planner import (
    prepaid_balance_enforcement_enabled,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_enforcement_readiness import (
    prepaid_enforcement_readiness_block_reason,
)
from scripts.one_off.billing_alignment_audit import _configure_read_only_session


@dataclass(frozen=True)
class AcceptanceExpectation:
    git_sha: str
    alembic_head: str
    minimum_active_baselines: int
    coverage_fingerprint: str
    coverage_subscription_count: int
    renewal_control_enabled: bool


@dataclass(frozen=True)
class AcceptanceObservation:
    git_sha: str | None
    alembic_heads: tuple[str, ...]
    active_baselines: int
    authority_cutover_batches: int
    active_readiness_records: int
    enforcement_enabled: bool
    renewal_control_enabled: bool
    activation_at: str | None
    activation_error: str | None
    readiness_block_reason: str | None
    coverage_fingerprint: str
    coverage_subscription_count: int
    coverage_repairable_count: int
    coverage_quarantined_count: int
    coverage_blocker_count: int


def _runtime_git_sha() -> str | None:
    return os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA")


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _collect_observation(db: Session, *, as_of: datetime) -> AcceptanceObservation:
    policy = resolve_prepaid_enforcement_policy(db)
    coverage = preview_prepaid_coverage_reconciliation(db, as_of=as_of)
    alembic_heads = tuple(
        sorted(
            str(value)
            for value in db.scalars(text("SELECT version_num FROM alembic_version"))
        )
    )
    return AcceptanceObservation(
        git_sha=_runtime_git_sha(),
        alembic_heads=alembic_heads,
        active_baselines=int(
            db.scalar(
                select(func.count(PrepaidFundingBaseline.id)).where(
                    PrepaidFundingBaseline.is_active.is_(True)
                )
            )
            or 0
        ),
        authority_cutover_batches=int(
            db.scalar(
                select(func.count(PrepaidFundingReconstructionBatch.id)).where(
                    PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True)
                )
            )
            or 0
        ),
        active_readiness_records=int(
            db.scalar(
                select(func.count(PrepaidEnforcementReadiness.id)).where(
                    PrepaidEnforcementReadiness.is_active.is_(True)
                )
            )
            or 0
        ),
        enforcement_enabled=prepaid_balance_enforcement_enabled(db),
        renewal_control_enabled=control_registry.is_enabled(
            db, "billing.prepaid_service_renewals"
        ),
        activation_at=(
            policy.activation_at.isoformat()
            if policy.activation_at is not None
            else None
        ),
        activation_error=policy.activation_error,
        readiness_block_reason=prepaid_enforcement_readiness_block_reason(db),
        coverage_fingerprint=coverage.fingerprint,
        coverage_subscription_count=len(coverage.subscription_ids),
        coverage_repairable_count=coverage.repairable_count,
        coverage_quarantined_count=coverage.quarantined_count,
        coverage_blocker_count=coverage.blocker_count,
    )


def evaluate_acceptance(
    observation: AcceptanceObservation,
    expectation: AcceptanceExpectation,
) -> dict[str, Any]:
    checks = {
        "git_sha": observation.git_sha == expectation.git_sha,
        "single_alembic_head": observation.alembic_heads == (expectation.alembic_head,),
        "minimum_active_baselines": (
            observation.active_baselines >= expectation.minimum_active_baselines
        ),
        "single_authority_cutover": observation.authority_cutover_batches == 1,
        "single_active_readiness": observation.active_readiness_records == 1,
        "enforcement_enabled": observation.enforcement_enabled,
        "renewal_control_state": (
            observation.renewal_control_enabled == expectation.renewal_control_enabled
        ),
        "activation_valid": (
            observation.activation_at is not None
            and observation.activation_error is None
        ),
        "readiness_unblocked": observation.readiness_block_reason is None,
        "coverage_fingerprint": (
            observation.coverage_fingerprint == expectation.coverage_fingerprint
        ),
        "coverage_subscription_count": (
            observation.coverage_subscription_count
            == expectation.coverage_subscription_count
        ),
        "coverage_repair_complete": observation.coverage_repairable_count == 0,
        "coverage_quarantine_empty": observation.coverage_quarantined_count == 0,
        "coverage_unblocked": observation.coverage_blocker_count == 0,
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "expected": asdict(expectation),
        "observed": asdict(observation),
    }


def _expectation(args: argparse.Namespace) -> AcceptanceExpectation:
    if args.minimum_active_baselines < 1:
        raise ValueError("minimum active baselines must be positive")
    if args.expected_subscription_count < 1:
        raise ValueError("expected subscription count must be positive")
    fingerprint = args.expected_coverage_fingerprint.strip()
    if len(fingerprint) != 64:
        raise ValueError("expected coverage fingerprint must contain 64 characters")
    return AcceptanceExpectation(
        git_sha=args.expected_git_sha.strip(),
        alembic_head=args.expected_alembic_head.strip(),
        minimum_active_baselines=args.minimum_active_baselines,
        coverage_fingerprint=fingerprint,
        coverage_subscription_count=args.expected_subscription_count,
        renewal_control_enabled=args.expected_renewal_control == "enabled",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=_timestamp, required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--expected-alembic-head", required=True)
    parser.add_argument("--minimum-active-baselines", type=int, required=True)
    parser.add_argument("--expected-coverage-fingerprint", required=True)
    parser.add_argument("--expected-subscription-count", type=int, required=True)
    parser.add_argument(
        "--expected-renewal-control",
        choices=("enabled", "disabled"),
        required=True,
    )
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--statement-timeout-ms", type=int, default=300000)
    args = parser.parse_args()

    expectation = _expectation(args)
    db = SessionLocal()
    try:
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        observation = _collect_observation(db, as_of=args.as_of)
        report = evaluate_acceptance(observation, expectation)
        db.rollback()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["ready"] else 2
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
