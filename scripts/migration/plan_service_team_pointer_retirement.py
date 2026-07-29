"""Build one private, read-only legacy pointer-retirement plan."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.db import SessionLocal
from app.services.service_team_pointer_retirement import (
    ServiceTeamPointerRetirementError,
    build_pointer_retirement_plan,
)


def _write_private_json(path: Path, payload: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        with SessionLocal() as db:
            plan = build_pointer_retirement_plan(
                db,
                planned_at=datetime.now(UTC),
            )
            db.rollback()
        _write_private_json(args.out, plan.file_payload())
    except (OSError, ServiceTeamPointerRetirementError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "planned",
                "plan_digest": plan.plan_digest,
                "pointer_count": len(plan.pointers),
                "source_snapshot_sha256": plan.source_snapshot_sha256,
                "output": str(args.out),
                "database_writes": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
