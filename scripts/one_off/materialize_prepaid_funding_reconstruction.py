"""Materialize a reviewed full-cohort prepaid funding reconstruction.

The input is the complete JSON emitted by ``export_prepaid_funding_snapshot``.
Dry-run is the default. Apply requires the independently reviewed normalized
manifest hash, a non-secret evidence reference, an approving actor, and an
explicit acknowledgement that the authority cutover is final.

This command stores account IDs, currency, balances, hashes, timestamps, and a
non-secret evidence pointer. It never stores bank-statement rows, credentials,
customer identity text, or narrations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.services.prepaid_enforcement_planner import candidate_prepaid_account_ids
from app.services.prepaid_funding_reconstruction import (
    apply_prepaid_funding_reconstruction,
    preview_prepaid_funding_reconstruction,
)

FINAL_CUTOVER_CONFIRMATION = "MATERIALIZE_VERIFIED_PREPAID_FUNDING"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("reconstruction manifest must be a JSON object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-sha256")
    parser.add_argument("--evidence-ref")
    parser.add_argument("--approved-by")
    parser.add_argument("--confirm-final-cutover")
    args = parser.parse_args()

    if args.apply:
        required = {
            "--reviewed-sha256": args.reviewed_sha256,
            "--evidence-ref": args.evidence_ref,
            "--approved-by": args.approved_by,
        }
        missing = [
            name for name, value in required.items() if not str(value or "").strip()
        ]
        if missing:
            parser.error("--apply requires " + ", ".join(missing))
        if args.confirm_final_cutover != FINAL_CUTOVER_CONFIRMATION:
            parser.error(
                "--apply requires --confirm-final-cutover " + FINAL_CUTOVER_CONFIRMATION
            )

    payload = _load_manifest(args.manifest)
    db = SessionLocal()
    try:
        expected_ids = set(candidate_prepaid_account_ids(db))
        preview = preview_prepaid_funding_reconstruction(
            db,
            payload,
            expected_account_ids=expected_ids,
        )
        print(json.dumps(preview.report(), indent=2, sort_keys=True))
        if not preview.ready:
            print("BLOCKED - reconstruction was not materialized")
            return 2
        if not args.apply:
            print("DRY RUN - no database state changed")
            return 0

        result = apply_prepaid_funding_reconstruction(
            db,
            payload,
            expected_manifest_sha256=str(args.reviewed_sha256),
            evidence_ref=str(args.evidence_ref),
            approved_by=str(args.approved_by),
            expected_account_ids=expected_ids,
        )
        db.commit()
        print(
            json.dumps(
                {
                    "batch_id": str(result.batch.id),
                    "manifest_sha256": result.batch.manifest_sha256,
                    "account_count": result.batch.account_count,
                    "idempotent_replay": result.idempotent_replay,
                    "authority": "verified_reconstruction",
                    "legacy_fallback": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
