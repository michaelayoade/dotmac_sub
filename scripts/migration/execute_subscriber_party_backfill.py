#!/usr/bin/env python3
"""Execute one separately approved, digest-bound Subscriber Party backfill.

This command is intentionally unavailable without an exact private decision
file, generated plan summary, expiring approval envelope, typed plan digest,
and explicit ``--execute`` acknowledgement. It never merges or repoints an
identity, assigns a role, or changes subscription/billing/access state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.services.party_identity_adjudication import PartyAdjudicationError
from app.services.party_identity_backfill import (
    PartyBackfillExecutionApproval,
    PartyIdentityBackfillError,
    execute_party_backfill_transaction,
)
from scripts.migration.plan_subscriber_party_backfill import (
    DecisionFileError,
    load_decisions,
)


class ExecutionFileError(ValueError):
    pass


def _assert_private_file(path: Path, field_name: str) -> None:
    if path.is_symlink():
        raise ExecutionFileError(f"{field_name} '{path}' must not be a symlink")
    if not path.is_file():
        raise ExecutionFileError(f"{field_name} '{path}' is not a file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode != 0o600:
        raise ExecutionFileError(
            f"{field_name} '{path}' must have mode 0o600; current mode is {oct(mode)}"
        )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, field_name: str) -> dict[str, Any]:
    _assert_private_file(path, field_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionFileError(f"{field_name} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ExecutionFileError(f"{field_name} must contain a JSON object")
    return payload


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    cleaned = value.strip() if isinstance(value, str) else ""
    if not cleaned:
        raise ExecutionFileError(f"approval.{field_name} is required")
    return cleaned


def _required_datetime(payload: dict[str, Any], field_name: str) -> datetime:
    raw = _required_text(payload, field_name)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionFileError(
            f"approval.{field_name} must be an ISO-8601 datetime"
        ) from exc


def _required_count(payload: dict[str, Any], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionFileError(f"approval.{field_name} must be a nonnegative integer")
    return value


def load_approval(path: Path) -> PartyBackfillExecutionApproval:
    payload = _load_json(path, "approval file")
    if payload.get("contract_version") != 1:
        raise ExecutionFileError("approval.contract_version must be 1")
    return PartyBackfillExecutionApproval(
        plan_digest=_required_text(payload, "plan_digest"),
        audit_digest=_required_text(payload, "audit_digest"),
        decision_file_sha256=_required_text(payload, "decision_file_sha256"),
        plan_file_sha256=_required_text(payload, "plan_file_sha256"),
        approved_by=_required_text(payload, "approved_by"),
        approved_at=_required_datetime(payload, "approved_at"),
        expires_at=_required_datetime(payload, "expires_at"),
        reason=_required_text(payload, "reason"),
        maximum_parties=_required_count(payload, "maximum_parties"),
        maximum_bindings=_required_count(payload, "maximum_bindings"),
    )


def _validate_plan_summary(
    payload: dict[str, Any],
    *,
    approval: PartyBackfillExecutionApproval,
    decision_file_sha256: str,
) -> None:
    contract = payload.get("artifact_contract")
    errors: list[str] = []
    if not isinstance(contract, dict):
        errors.append("plan artifact contract is missing")
    else:
        if contract.get("automatic_merge_allowed") is not False:
            errors.append("plan artifact does not prohibit automatic merge")
        if contract.get("execution_requires_separate_approval") is not True:
            errors.append("plan artifact lacks the separate-approval gate")
    expected = {
        "plan_digest": approval.plan_digest,
        "audit_digest": approval.audit_digest,
        "decision_file_sha256": decision_file_sha256,
    }
    for field_name, value in expected.items():
        if payload.get(field_name) != value:
            errors.append(f"plan {field_name} does not match the approved input")
    if payload.get("planned_parties") != approval.maximum_parties:
        errors.append("approval maximum_parties must exactly match the plan count")
    if payload.get("planned_bindings") != approval.maximum_bindings:
        errors.append("approval maximum_bindings must exactly match the plan count")
    raw_planned_at = payload.get("planned_at")
    try:
        planned_at = datetime.fromisoformat(str(raw_planned_at).replace("Z", "+00:00"))
    except ValueError:
        errors.append("plan planned_at is not a valid ISO-8601 datetime")
    else:
        if planned_at.tzinfo is None:
            errors.append("plan planned_at must be timezone-aware")
        elif approval.approved_at.tzinfo is None:
            errors.append("approval approved_at must be timezone-aware")
        elif approval.approved_at < planned_at:
            errors.append("approval predates the generated plan")
    if errors:
        raise ExecutionFileError("; ".join(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--confirm-plan-digest", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Acknowledge that this command will commit the exact approved plan",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.execute:
        print("REFUSED: --execute acknowledgement is required")
        return 2
    try:
        _assert_private_file(args.decisions, "decision file")
        decision_file_sha256 = _file_sha256(args.decisions)
        _assert_private_file(args.plan, "plan file")
        plan_file_sha256 = _file_sha256(args.plan)
        plan_payload = _load_json(args.plan, "plan file")
        _assert_private_file(args.approval, "approval file")
        approval_file_sha256 = _file_sha256(args.approval)
        approval = load_approval(args.approval)
        if args.confirm_plan_digest != approval.plan_digest:
            raise ExecutionFileError(
                "typed --confirm-plan-digest does not match the approval"
            )
        if plan_file_sha256 != approval.plan_file_sha256:
            raise ExecutionFileError("plan file SHA-256 does not match the approval")
        _validate_plan_summary(
            plan_payload,
            approval=approval,
            decision_file_sha256=decision_file_sha256,
        )
        decisions = load_decisions(args.decisions)
        if (
            _file_sha256(args.decisions) != decision_file_sha256
            or _file_sha256(args.plan) != plan_file_sha256
            or _file_sha256(args.approval) != approval_file_sha256
        ):
            raise ExecutionFileError(
                "an execution input file changed while it was being validated"
            )
    except (DecisionFileError, ExecutionFileError, ValueError) as exc:
        print(f"REFUSED: {exc}")
        return 2

    with SessionLocal() as db:
        try:
            outcome = execute_party_backfill_transaction(
                db,
                decisions=decisions,
                approval=approval,
                decision_file_sha256=decision_file_sha256,
                plan_file_sha256=plan_file_sha256,
                approval_file_sha256=approval_file_sha256,
            )
        except (
            DecisionFileError,
            PartyAdjudicationError,
            PartyIdentityBackfillError,
        ) as exc:
            print(f"REFUSED: {exc}")
            return 2
        except SQLAlchemyError:
            print(
                "FAILED: database execution failed and the transaction was rolled back"
            )
            return 1

    print(
        json.dumps(
            {
                "status": "replayed" if outcome.replayed else "applied",
                "receipt_id": str(outcome.receipt_id),
                "plan_digest": outcome.plan_digest,
                "parties_created": outcome.parties_created,
                "bindings_created": outcome.bindings_created,
                "automatic_merges": 0,
                "repoints": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
