"""Compile a reviewed, read-only database authority plan from two JSON files.

This command cannot connect to PostgreSQL or execute the emitted statements.
Its input is a bounded catalog observation and a separate object policy.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from app.database_role_plan import (
    PLAN_VERSION,
    PlanInputError,
    compile_plan,
    decode_catalog,
    decode_policy,
)

MAX_INPUT_BYTES = 8 * 1024 * 1024


def _read_json(path: Path) -> object:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise PlanInputError("input exceeds bounded file size")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        catalog = decode_catalog(_read_json(args.catalog))
        policy = decode_policy(_read_json(args.policy))
        result = asdict(compile_plan(catalog, policy))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        PlanInputError,
    ):
        # Do not echo file content or raw parser exceptions: catalog inputs
        # contain identifiers, and no arbitrary text belongs in a CLI log.
        result = {
            "schema_version": PLAN_VERSION,
            "status": "blocked",
            "database": None,
            "target_owner": None,
            "blocked_reasons": ["supplied catalog or policy is invalid or unreadable"],
            "statements": [],
            "catalog_sha256": None,
            "policy_sha256": None,
            "plan_sha256": None,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "ready_for_review" else 2


if __name__ == "__main__":
    raise SystemExit(main())
