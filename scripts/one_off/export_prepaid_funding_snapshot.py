"""Export independently reconstructed prepaid funding facts for owner planning.

This command is the bridge between the billing alignment replay and
``plan_prepaid_balance_sweep.py --funding-snapshot``. It does not decide who is
prepaid, how much funding is required, whether an account is shielded, or what
access consequence follows:

* ``financial.prepaid_enforcement`` selects the candidate cohort;
* the alignment replay reconstructs available funding from the final Splynx
  position plus proven post-legacy facts;
* ``financial.prepaid_threshold`` supplies the canonical required balance;
* the enforcement planner applies profile, activation, grace, shield, health,
  and lifecycle policy.

The export is complete-or-error. If any candidate lacks a source baseline or
has an incomplete replay, no planner-consumable funding file is written. An
optional blocker manifest contains UUIDs and reason codes only—no customer
identity, credentials, free text, or delivery coordinates.

Safety: this command has no apply mode, sets its PostgreSQL transaction read
only, requires an explicitly approved primary override for an ephemeral restore,
and always rolls the session back.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services.prepaid_enforcement_planner import candidate_prepaid_account_ids
from app.services.prepaid_threshold import resolve_prepaid_thresholds
from scripts.one_off.billing_alignment_audit import (
    _batch_reconstructed_positions,
    _configure_read_only_session,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("snapshot timestamp must include a timezone offset")
    return value.astimezone(UTC)


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


@dataclass(frozen=True)
class FundingSnapshotExport:
    captured_at: datetime
    source: str
    candidate_ids: tuple[str, ...]
    positions: dict[str, Decimal]
    thresholds: dict[str, Decimal]
    incomplete: dict[str, tuple[str, ...]]
    missing_baseline: tuple[str, ...]
    missing_threshold: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not (self.incomplete or self.missing_baseline or self.missing_threshold)

    def funding_payload(self) -> dict[str, Any]:
        if not self.ready:
            raise ValueError("funding snapshot is blocked by incomplete provenance")
        return {
            "source": self.source,
            "captured_at": self.captured_at.isoformat().replace("+00:00", "Z"),
            "accounts": [
                {
                    "account_id": account_id,
                    "available_balance": _money(self.positions[account_id]),
                    "required_balance": _money(self.thresholds[account_id]),
                }
                for account_id in self.candidate_ids
            ],
        }

    def diagnostics_payload(self) -> dict[str, Any]:
        reason_counts = Counter(
            reason for reasons in self.incomplete.values() for reason in reasons
        )
        return {
            "source": self.source,
            "captured_at": self.captured_at.isoformat().replace("+00:00", "Z"),
            "ready": self.ready,
            "candidate_accounts": len(self.candidate_ids),
            "reconstructed_accounts": len(self.positions),
            "blocker_counts": {
                "incomplete_replay": len(self.incomplete),
                "missing_source_baseline": len(self.missing_baseline),
                "missing_threshold": len(self.missing_threshold),
            },
            "incomplete_reason_counts": dict(sorted(reason_counts.items())),
            "blockers": {
                "incomplete_replay": [
                    {"account_id": account_id, "reasons": list(reasons)}
                    for account_id, reasons in sorted(self.incomplete.items())
                ],
                "missing_source_baseline": list(self.missing_baseline),
                "missing_threshold": list(self.missing_threshold),
            },
        }


def build_prepaid_funding_snapshot(
    db: Session,
    *,
    snapshot_at: datetime,
    source: str,
) -> FundingSnapshotExport:
    """Build a complete candidate snapshot without consulting mutable outputs."""
    captured_at = _as_utc(snapshot_at)
    source_label = source.strip()
    if not source_label:
        raise ValueError("source label must not be empty")

    candidate_ids = tuple(
        sorted((str(value) for value in candidate_prepaid_account_ids(db)), key=str)
    )
    replay = _batch_reconstructed_positions(
        db,
        list(candidate_ids),
        snapshot_at=captured_at,
    )
    thresholds = resolve_prepaid_thresholds(
        db,
        list(candidate_ids),
        now=captured_at,
    )

    candidate_set = set(candidate_ids)
    positions = {
        account_id: amount
        for account_id, amount in replay.positions.items()
        if account_id in candidate_set
    }
    incomplete = {
        account_id: tuple(sorted(reasons))
        for account_id, reasons in replay.incomplete.items()
        if account_id in candidate_set
    }
    missing_baseline = tuple(
        account_id for account_id in candidate_ids if account_id not in positions
    )
    missing_threshold = tuple(
        account_id for account_id in candidate_ids if account_id not in thresholds
    )
    return FundingSnapshotExport(
        captured_at=captured_at,
        source=source_label,
        candidate_ids=candidate_ids,
        positions=positions,
        thresholds={
            account_id: thresholds[account_id]
            for account_id in candidate_ids
            if account_id in thresholds
        },
        incomplete=incomplete,
        missing_baseline=missing_baseline,
        missing_threshold=missing_threshold,
    )


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _require_ephemeral_postgres(db: Session) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        raise RuntimeError("export requires the isolated PostgreSQL audit restore")
    if os.getenv("BILLING_AUDIT_EPHEMERAL") != "1":
        raise RuntimeError("set BILLING_AUDIT_EPHEMERAL=1 for the isolated audit DB")
    database_name = str(db.scalar(text("SELECT current_database()")) or "")
    if not database_name.endswith("_audit"):
        raise RuntimeError(
            "refusing non-audit database; current_database() must end in _audit"
        )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return _as_utc(parsed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-at", type=_parse_timestamp, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--blockers-out", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=60000,
        help="PostgreSQL timeout per query (default: 60000)",
    )
    parser.add_argument(
        "--allow-primary",
        action="store_true",
        help="allow the explicitly approved ephemeral audit primary",
    )
    args = parser.parse_args()
    if args.statement_timeout_ms <= 0:
        parser.error("--statement-timeout-ms must be greater than zero")

    db = SessionLocal()
    try:
        _require_ephemeral_postgres(db)
        _configure_read_only_session(
            db,
            statement_timeout_ms=args.statement_timeout_ms,
            allow_primary=args.allow_primary,
        )
        export = build_prepaid_funding_snapshot(
            db,
            snapshot_at=args.snapshot_at,
            source=args.source,
        )
        diagnostics = export.diagnostics_payload()
        print(
            json.dumps(
                {key: value for key, value in diagnostics.items() if key != "blockers"},
                indent=2,
                sort_keys=True,
            )
        )
        if args.blockers_out is not None:
            _write_json(args.blockers_out, diagnostics, overwrite=args.overwrite)
        if not export.ready:
            print("BLOCKED - no planner-consumable funding snapshot was written")
            return 2
        _write_json(args.out, export.funding_payload(), overwrite=args.overwrite)
        print(f"Funding snapshot written: {args.out}")
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
