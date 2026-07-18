"""Fail-closed acceptance check for the prepaid renewal deployment.

This command composes existing owners; it does not derive balances, choose an
enforcement consequence, or write evidence. It verifies that the expected app
revision and database head are running, the materialized funding authority and
readiness gate remain intact, enforcement is enabled, and the exact reviewed
service-cycle plan still previews ready through
``financial.prepaid_service_renewals``.

Only aggregate state is emitted. The reviewed plan may contain UUIDs, but this
command never prints its entries. PostgreSQL sessions are read-only, primary
execution requires ``--allow-primary``, and the session is always rolled back.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
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
from app.services.prepaid_enforcement_planner import (
    prepaid_balance_enforcement_enabled,
    resolve_prepaid_enforcement_policy,
)
from app.services.prepaid_enforcement_readiness import (
    prepaid_enforcement_readiness_block_reason,
)
from scripts.one_off.billing_alignment_audit import _configure_read_only_session
from scripts.one_off.reconcile_prepaid_service_cycle_gaps import (
    ServiceCyclePlan,
    _money,
    _read_json,
    parse_reconciliation_plan,
    preview_reconciliation,
)


@dataclass(frozen=True)
class AcceptanceExpectation:
    git_sha: str
    alembic_head: str
    minimum_active_baselines: int
    plan_sha256: str
    plan_entry_count: int
    plan_total_amount: str
    plan_already_reconciled: int
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
    plan_sha256: str
    plan_entries: int
    plan_total_amount: str
    plan_accounts: int
    plan_blocked_accounts: int
    plan_already_reconciled: int
    plan_ready: bool


def _runtime_git_sha() -> str | None:
    return os.getenv("GIT_SHA") or os.getenv("COMMIT_SHA")


def _collect_observation(db: Session, plan: ServiceCyclePlan) -> AcceptanceObservation:
    policy = resolve_prepaid_enforcement_policy(db)
    preview = preview_reconciliation(db, plan)
    alembic_heads = tuple(
        sorted(str(value) for value in db.scalars(text("SELECT version_num FROM alembic_version")))
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
            policy.activation_at.isoformat() if policy.activation_at is not None else None
        ),
        activation_error=policy.activation_error,
        readiness_block_reason=prepaid_enforcement_readiness_block_reason(db),
        plan_sha256=str(preview["plan_sha256"]),
        plan_entries=int(str(preview["entries"])),
        plan_total_amount=str(preview["total_amount"]),
        plan_accounts=int(str(preview["accounts"])),
        plan_blocked_accounts=int(str(preview["blocked_accounts"])),
        plan_already_reconciled=int(str(preview["already_reconciled"])),
        plan_ready=bool(preview["ready"]),
    )


def evaluate_acceptance(
    observation: AcceptanceObservation,
    expectation: AcceptanceExpectation,
) -> dict[str, Any]:
    checks = {
        "git_sha": observation.git_sha == expectation.git_sha,
        "single_alembic_head": observation.alembic_heads
        == (expectation.alembic_head,),
        "minimum_active_baselines": (
            observation.active_baselines >= expectation.minimum_active_baselines
        ),
        "single_authority_cutover": observation.authority_cutover_batches == 1,
        "single_active_readiness": observation.active_readiness_records == 1,
        "enforcement_enabled": observation.enforcement_enabled,
        "renewal_control_state": (
            observation.renewal_control_enabled
            == expectation.renewal_control_enabled
        ),
        "activation_valid": (
            observation.activation_at is not None
            and observation.activation_error is None
        ),
        "readiness_unblocked": observation.readiness_block_reason is None,
        "plan_sha256": observation.plan_sha256 == expectation.plan_sha256,
        "plan_entry_count": (
            observation.plan_entries == expectation.plan_entry_count
        ),
        "plan_total_amount": (
            _money(observation.plan_total_amount)
            == _money(expectation.plan_total_amount)
        ),
        "plan_unblocked": (
            observation.plan_ready and observation.plan_blocked_accounts == 0
        ),
        "plan_reconciliation_state": (
            observation.plan_already_reconciled
            == expectation.plan_already_reconciled
        ),
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
    if args.expected_entry_count < 1:
        raise ValueError("expected entry count must be positive")
    if not 0 <= args.expected_already_reconciled <= args.expected_entry_count:
        raise ValueError(
            "expected already-reconciled count must be within the plan"
        )
    if len(args.expected_plan_sha256) != 64:
        raise ValueError("expected plan SHA-256 must contain 64 characters")
    return AcceptanceExpectation(
        git_sha=args.expected_git_sha.strip(),
        alembic_head=args.expected_alembic_head.strip(),
        minimum_active_baselines=args.minimum_active_baselines,
        plan_sha256=args.expected_plan_sha256.strip(),
        plan_entry_count=args.expected_entry_count,
        plan_total_amount=f"{_money(args.expected_total_amount):.2f}",
        plan_already_reconciled=args.expected_already_reconciled,
        renewal_control_enabled=args.expected_renewal_control == "enabled",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--expected-alembic-head", required=True)
    parser.add_argument("--minimum-active-baselines", type=int, required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--expected-entry-count", type=int, required=True)
    parser.add_argument("--expected-total-amount", type=Decimal, required=True)
    parser.add_argument("--expected-already-reconciled", type=int, default=0)
    parser.add_argument(
        "--expected-renewal-control",
        choices=("enabled", "disabled"),
        required=True,
    )
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--statement-timeout-ms", type=int, default=300000)
    args = parser.parse_args()

    expectation = _expectation(args)
    plan = parse_reconciliation_plan(_read_json(args.plan))
    db = SessionLocal()
    try:
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        observation = _collect_observation(db, plan)
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
