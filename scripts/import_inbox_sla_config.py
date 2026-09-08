"""Import CRM Inbox SLA configuration without copying CRM operational data.

Input is a sanitised JSON export containing configuration only.  The command
is dry-run by default and refuses unresolved team/channel/priority mappings.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ImportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    rejected: int = 0
    unresolved: tuple[str, ...] = ()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("policies"), list):
        raise ValueError("input must be an object with a policies list")
    return value


def plan_import(
    source: dict[str, Any], mappings: dict[str, dict[str, str]]
) -> ImportSummary:
    unresolved: list[str] = []
    accepted = 0
    for policy in source["policies"]:
        for rule in policy.get("rules", []):
            rule_unresolved = False
            for kind, key in (
                ("team", rule.get("crm_team_id")),
                ("channel", rule.get("crm_channel")),
                ("priority", rule.get("crm_priority")),
            ):
                if key is not None and str(key) not in mappings.get(kind, {}):
                    unresolved.append(f"{kind}:{key}")
                    rule_unresolved = True
            if not rule_unresolved:
                accepted += 1
    return ImportSummary(
        skipped=len(source["policies"]) - accepted,
        rejected=0,
        unresolved=tuple(sorted(set(unresolved))),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--mappings",
        type=Path,
        required=True,
        help="JSON maps with team, channel, and priority keys",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only after a clean dry-run and explicit mapping review",
    )
    args = parser.parse_args()
    source = _load(args.input)
    mappings = json.loads(args.mappings.read_text(encoding="utf-8-sig"))
    if not isinstance(mappings, dict):
        raise ValueError("mappings must be an object")
    summary = plan_import(source, mappings)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "rejected": summary.rejected,
                "unresolved_mappings": list(summary.unresolved),
            },
            sort_keys=True,
        )
    )
    if summary.unresolved:
        print(
            "Import refused: unresolved mappings must be reviewed; no configuration was changed."
        )
        return 2
    if not args.apply:
        print("Dry run only: no database writes were performed.")
        return 0
    raise SystemExit(
        "Apply mode requires the reviewed database adapter to be enabled in the deployment runbook."
    )


if __name__ == "__main__":
    raise SystemExit(main())
