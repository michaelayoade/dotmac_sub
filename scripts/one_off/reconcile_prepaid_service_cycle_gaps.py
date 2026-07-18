"""Export and reconcile exact post-cutover prepaid service-cycle gaps.

The audit replay may prove that a service cycle became due while the customer
had enough reviewed funding, yet find no canonical entitlement/debit for that
period. This tool bridges that evidence to the named runtime owner without
hardcoding an account cohort:

* export mode runs read-only on the isolated audit restore and writes a
  content-addressed UUID/amount/period plan;
* execution mode is dry-run by default and rechecks current canonical funding;
* apply requires the exact reviewed SHA-256 plus two explicit confirmations;
* every write goes through ``financial.prepaid_service_renewals`` and the whole
  reviewed plan commits atomically.

No customer names, usernames, contact fields, credentials, or free-text source
data are emitted. The plan contains UUIDs, dates, amounts, reason codes, and
non-secret evidence hashes only.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.common import round_money
from app.services.prepaid_funding_attestation import (
    candidate_cohort_sha256,
    canonical_payload_sha256,
)
from scripts.one_off.billing_alignment_audit import _configure_read_only_session
from scripts.one_off.export_prepaid_funding_snapshot import (
    _require_ephemeral_postgres,
    build_prepaid_funding_snapshot,
)

PLAN_SCHEMA = "dotmac.prepaid_service_cycle_reconciliation.v1"
GAP_REASON = "due_service_charge_without_native_entitlement"
APPLY_CONFIRMATION = "RECONCILE_REVIEWED_PREPAID_SERVICE_CYCLES"
_PLAN_FIELDS = {
    "schema",
    "captured_at",
    "source",
    "currency",
    "candidate_cohort_sha256",
    "blocker_manifest_sha256",
    "entry_count",
    "total_amount",
    "entries",
}
_ENTRY_FIELDS = {
    "account_id",
    "subscription_id",
    "period_start",
    "period_end",
    "amount",
    "funding_before",
    "currency",
    "reason",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return value.astimezone(UTC)


def _timestamp(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))


def _money(value: object) -> Decimal:
    return round_money(Decimal(str(value)))


def _money_text(value: object) -> str:
    return f"{_money(value):.2f}"


def _canonical_sha256(payload: dict[str, Any]) -> str:
    return canonical_payload_sha256(payload)


@dataclass(frozen=True)
class ServiceCyclePlanEntry:
    account_id: str
    subscription_id: str
    period_start: datetime
    period_end: datetime
    amount: Decimal
    funding_before: Decimal
    currency: str
    reason: str


@dataclass(frozen=True)
class ServiceCyclePlan:
    payload: dict[str, Any]
    entries: tuple[ServiceCyclePlanEntry, ...]

    @property
    def sha256(self) -> str:
        return _canonical_sha256(self.payload)


def build_reconciliation_plan(
    db: Session,
    *,
    snapshot_at: datetime,
    source: str,
) -> dict[str, Any]:
    export = build_prepaid_funding_snapshot(
        db,
        snapshot_at=_utc(snapshot_at),
        source=source,
    )
    gaps = sorted(
        (
            gap
            for gap in export.service_cycle_gaps
            if gap.reason == GAP_REASON and gap.subscription_id is not None
        ),
        key=lambda gap: (
            gap.account_id,
            str(gap.subscription_id),
            gap.period_start,
        ),
    )
    entries = [
        {
            "account_id": gap.account_id,
            "subscription_id": str(gap.subscription_id),
            "period_start": datetime.combine(
                gap.period_start, datetime.min.time(), tzinfo=UTC
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "period_end": datetime.combine(
                gap.period_end, datetime.min.time(), tzinfo=UTC
            )
            .isoformat()
            .replace("+00:00", "Z"),
            "amount": _money_text(gap.amount),
            "funding_before": _money_text(gap.funding_before),
            "currency": gap.currency,
            "reason": gap.reason,
        }
        for gap in gaps
    ]
    if any(
        _money(entry["funding_before"]) < _money(entry["amount"]) for entry in entries
    ):
        raise RuntimeError("audit emitted an unaffordable service-cycle repair")
    candidate_ids = export.candidate_ids
    return {
        "schema": PLAN_SCHEMA,
        "captured_at": export.captured_at.isoformat().replace("+00:00", "Z"),
        "source": export.source,
        "currency": export.currency,
        "candidate_cohort_sha256": candidate_cohort_sha256(candidate_ids),
        "blocker_manifest_sha256": export.blocker_manifest_sha256,
        "entry_count": len(entries),
        "total_amount": _money_text(
            sum((_money(entry["amount"]) for entry in entries), Decimal("0.00"))
        ),
        "entries": entries,
    }


def parse_reconciliation_plan(payload: dict[str, Any]) -> ServiceCyclePlan:
    if set(payload) != _PLAN_FIELDS:
        raise ValueError("reconciliation plan fields are incomplete or unexpected")
    if payload.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported reconciliation plan schema")
    captured_at = _timestamp(str(payload.get("captured_at")))
    if captured_at > datetime.now(UTC):
        raise ValueError("reconciliation plan is future-dated")
    source = str(payload.get("source") or "").strip()
    if not source:
        raise ValueError("reconciliation plan source is required")
    currency = str(payload.get("currency") or "").strip().upper()
    if len(currency) != 3:
        raise ValueError("reconciliation plan currency is invalid")
    for hash_field in ("candidate_cohort_sha256", "blocker_manifest_sha256"):
        value = str(payload.get(hash_field) or "")
        if len(value) != 64:
            raise ValueError(f"{hash_field} must be a SHA-256")

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("reconciliation plan entries must be a list")
    entries: list[ServiceCyclePlanEntry] = []
    semantic_keys: set[tuple[str, str, datetime, datetime]] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != _ENTRY_FIELDS:
            raise ValueError("reconciliation entry fields are incomplete or unexpected")
        if raw.get("reason") != GAP_REASON:
            raise ValueError("reconciliation entry has an unsupported reason")
        period_start = _timestamp(str(raw.get("period_start")))
        period_end = _timestamp(str(raw.get("period_end")))
        if period_end <= period_start:
            raise ValueError("reconciliation entry period is invalid")
        amount = _money(raw.get("amount"))
        funding_before = _money(raw.get("funding_before"))
        if amount <= 0 or funding_before < amount:
            raise ValueError("reconciliation entry was not funded at period due")
        entry_currency = str(raw.get("currency") or "").strip().upper()
        if entry_currency != currency:
            raise ValueError("reconciliation entry currency does not match plan")
        account_id = str(raw.get("account_id") or "").strip()
        subscription_id = str(raw.get("subscription_id") or "").strip()
        semantic_key = (account_id, subscription_id, period_start, period_end)
        if not account_id or not subscription_id or semantic_key in semantic_keys:
            raise ValueError("reconciliation entry identity is missing or duplicated")
        semantic_keys.add(semantic_key)
        entries.append(
            ServiceCyclePlanEntry(
                account_id=account_id,
                subscription_id=subscription_id,
                period_start=period_start,
                period_end=period_end,
                amount=amount,
                funding_before=funding_before,
                currency=entry_currency,
                reason=GAP_REASON,
            )
        )
    if int(payload.get("entry_count", -1)) != len(entries):
        raise ValueError("reconciliation plan entry_count does not match entries")
    total = sum((entry.amount for entry in entries), Decimal("0.00"))
    if _money(payload.get("total_amount")) != round_money(total):
        raise ValueError("reconciliation plan total_amount does not match entries")
    return ServiceCyclePlan(payload=payload, entries=tuple(entries))


def preview_reconciliation(
    db: Session,
    plan: ServiceCyclePlan,
) -> dict[str, object]:
    from app.services.prepaid_service_renewals import preview_prepaid_service_renewal

    required_by_account: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    available_by_account: dict[str, Decimal] = {}
    blocked = 0
    already_reconciled = 0
    for entry in plan.entries:
        preview = preview_prepaid_service_renewal(
            db,
            subscription_id=entry.subscription_id,
            starts_at=entry.period_start,
            ends_at=entry.period_end,
            amount=entry.amount,
            currency=entry.currency,
        )
        if str(preview.account_id) != entry.account_id:
            raise ValueError("reconciliation entry subscription/account mismatch")
        if preview.replayed:
            already_reconciled += 1
            continue
        available_by_account.setdefault(entry.account_id, preview.funding_before)
        required_by_account[entry.account_id] += entry.amount
    for account_id, required in required_by_account.items():
        if available_by_account[account_id] < required:
            blocked += 1
    return {
        "plan_sha256": plan.sha256,
        "entries": len(plan.entries),
        "accounts": len(required_by_account),
        "total_amount": _money_text(
            sum((entry.amount for entry in plan.entries), Decimal("0.00"))
        ),
        "blocked_accounts": blocked,
        "already_reconciled": already_reconciled,
        "ready": blocked == 0,
    }


def apply_reconciliation(
    db: Session,
    plan: ServiceCyclePlan,
    *,
    evidence_ref: str,
    approved_by: str,
) -> dict[str, object]:
    from app.services.prepaid_service_renewals import (
        confirm_prepaid_service_renewal,
        preview_prepaid_service_renewal,
    )

    evidence = evidence_ref.strip()
    approver = approved_by.strip()
    if not evidence or not approver:
        raise ValueError("evidence_ref and approved_by are required")
    dry_run = preview_reconciliation(db, plan)
    if not dry_run["ready"]:
        raise RuntimeError("current canonical funding cannot cover the reviewed plan")
    applied = 0
    replayed = 0
    for entry in plan.entries:
        preview = preview_prepaid_service_renewal(
            db,
            subscription_id=entry.subscription_id,
            starts_at=entry.period_start,
            ends_at=entry.period_end,
            amount=entry.amount,
            currency=entry.currency,
        )
        result = confirm_prepaid_service_renewal(
            db,
            preview,
            evidence_ref=(
                f"{evidence};plan_sha256={plan.sha256};approved_by={approver}"
            ),
            commit=False,
        )
        if result.replayed:
            replayed += 1
        else:
            applied += 1
    db.commit()
    return {**dry_run, "applied": applied, "replayed": replayed}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("reconciliation plan root must be an object")
    return value


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite {path}")
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export-plan", type=Path)
    mode.add_argument("--plan", type=Path)
    parser.add_argument("--snapshot-at", type=_timestamp)
    parser.add_argument("--source")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument("--statement-timeout-ms", type=int, default=300000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-sha256")
    parser.add_argument("--evidence-ref")
    parser.add_argument("--approved-by")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.export_plan is not None:
            if args.apply:
                parser.error("--export-plan is read-only")
            if args.snapshot_at is None or not str(args.source or "").strip():
                parser.error("--export-plan requires --snapshot-at and --source")
            _require_ephemeral_postgres(db)
            _configure_read_only_session(
                db,
                statement_timeout_ms=args.statement_timeout_ms,
                allow_primary=args.allow_primary,
            )
            payload = build_reconciliation_plan(
                db,
                snapshot_at=args.snapshot_at,
                source=args.source,
            )
            plan = parse_reconciliation_plan(payload)
            _write_json(args.export_plan, payload, overwrite=args.overwrite)
            print(
                json.dumps(
                    {
                        "plan_sha256": plan.sha256,
                        "entries": len(plan.entries),
                        "total_amount": payload["total_amount"],
                        "blocker_manifest_sha256": payload["blocker_manifest_sha256"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        plan = parse_reconciliation_plan(_read_json(args.plan))
        if not args.apply:
            _configure_read_only_session(
                db,
                statement_timeout_ms=args.statement_timeout_ms,
                allow_primary=args.allow_primary,
            )
            result = preview_reconciliation(db, plan)
            db.rollback()
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ready"] else 2
        if args.reviewed_sha256 != plan.sha256:
            parser.error("--reviewed-sha256 does not match the plan")
        if args.confirm != APPLY_CONFIRMATION:
            parser.error(f"--confirm must be {APPLY_CONFIRMATION}")
        if not args.evidence_ref or not args.approved_by:
            parser.error("--apply requires --evidence-ref and --approved-by")
        result = apply_reconciliation(
            db,
            plan,
            evidence_ref=args.evidence_ref,
            approved_by=args.approved_by,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
