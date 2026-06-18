#!/usr/bin/env python
"""Apply the terminal-IP backlog cleanup — strictly gated (Track 3 part 2).

Deactivates the IPAssignment rows the planner classifies as SAFE
(safe_release_terminal + safe_dedupe_duplicate). conflict_* and manual_review
are NEVER touched. See docs/POST_CUTOVER_HARDENING.md.

DEFAULT IS DRY-RUN. ``--apply`` is required for any write, AND must be given a
``--expect`` manifest (from a prior dry-run) so the apply refuses if the plan
drifted between review and execution.

DO NOT run --apply in prod until: (1) #288 (the forward fix) has merged;
(2) the planner was re-run against current prod; (3) the safe counts were
reviewed; (4) finance/ops accepted the impact; (5) a rollback is ready — this
tool writes the exact released assignment ids and supports ``--rollback``.

Usage:
    # 1. dry-run: writes the plan manifest, changes nothing (default)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/network/apply_terminal_ip_backlog.py --out /app/plan.json

    # 2. apply, gated on the reviewed plan manifest
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/network/apply_terminal_ip_backlog.py \
            --apply --expect /app/plan.json --out /app/applied.json

    # rollback (re-activate the exact ids the apply manifest recorded)
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/network/apply_terminal_ip_backlog.py \
            --rollback /app/applied.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from app.db import SessionLocal
from app.services.ip_lifecycle import (
    APPLY_CLASSES,
    apply_backlog_cleanup,
    reactivate_assignments,
)

GATES = (
    "DO NOT run --apply in prod until: (1) #288 merged; (2) planner re-run vs "
    "current prod; (3) safe counts reviewed; (4) finance/ops accepted; "
    "(5) rollback ready (this manifest's released_assignment_ids)."
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write(path: str | None, payload: dict) -> None:
    if not path:
        return
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    print(f"manifest written: {path}")


def _print_counts(label: str, counts: dict) -> None:
    print(f"-- {label} --")
    for k in (
        *APPLY_CLASSES,
        "conflict_active_service",
        "conflict_management_or_ont",
        "manual_review",
    ):
        print(f"  {k:30s} {counts.get(k, 0):>5}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="Write. Requires --expect.")
    p.add_argument("--expect", help="Prior dry-run manifest to gate --apply against.")
    p.add_argument(
        "--allow-count-drift",
        action="store_true",
        help="Proceed even if the plan drifted from --expect (use with care).",
    )
    p.add_argument("--out", help="Where to write the result manifest.")
    p.add_argument("--rollback", help="Apply manifest whose ids to re-activate.")
    args = p.parse_args()

    db = SessionLocal()
    try:
        # --- rollback -------------------------------------------------------
        if args.rollback:
            with open(args.rollback) as fh:
                manifest = json.load(fh)
            ids = manifest.get("released_assignment_ids", [])
            n = reactivate_assignments(db, ids)
            print(f"ROLLBACK: re-activated {n}/{len(ids)} assignments")
            return 0

        # --- dry-run (default) ---------------------------------------------
        if not args.apply:
            res = apply_backlog_cleanup(db, dry_run=True)
            print("=== terminal-IP backlog cleanup — DRY RUN ===")
            _print_counts("plan counts", res["counts_before"])
            print(f"\nsafe targets (would release): {res['target_count']}")
            print(f"\n{GATES}")
            _write(
                args.out,
                {
                    "mode": "dry_run",
                    "generated_at": _now(),
                    "allowed_classes": res["allowed_classes"],
                    "counts_before": res["counts_before"],
                    "target_count": res["target_count"],
                    "target_assignment_ids": res["target_assignment_ids"],
                },
            )
            print("\nDRY RUN — nothing changed.")
            return 0

        # --- apply (gated) --------------------------------------------------
        if not args.expect:
            print("REFUSED: --apply requires --expect <prior dry-run manifest>.")
            return 2
        with open(args.expect) as fh:
            expected = json.load(fh)

        # Re-derive now and compare to the reviewed plan.
        current = apply_backlog_cleanup(db, dry_run=True)
        exp_ids = set(expected.get("target_assignment_ids", []))
        cur_ids = set(current["target_assignment_ids"])
        if exp_ids != cur_ids:
            added = sorted(cur_ids - exp_ids)
            removed = sorted(exp_ids - cur_ids)
            print(
                f"PLAN DRIFT vs --expect: +{len(added)} new / -{len(removed)} gone "
                f"target assignments since review."
            )
            if not args.allow_count_drift:
                print(
                    "REFUSED. Re-review and re-run dry-run, or pass "
                    "--allow-count-drift to override."
                )
                return 3
            print("--allow-count-drift set — proceeding on the CURRENT plan.")

        res = apply_backlog_cleanup(db, dry_run=False)
        print("=== terminal-IP backlog cleanup — APPLIED ===")
        _print_counts("counts before", res["counts_before"])
        _print_counts("counts after", res.get("counts_after", {}))
        print(f"\nreleased: {res['released']} assignments")
        _write(
            args.out,
            {
                "mode": "apply",
                "generated_at": _now(),
                "expect_manifest": args.expect,
                "allow_count_drift": args.allow_count_drift,
                "allowed_classes": res["allowed_classes"],
                "counts_before": res["counts_before"],
                "counts_after": res.get("counts_after", {}),
                "released": res["released"],
                "released_assignment_ids": res["released_assignment_ids"],
            },
        )
        print("\nROLLBACK: re-run with --rollback <this manifest> to undo.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
