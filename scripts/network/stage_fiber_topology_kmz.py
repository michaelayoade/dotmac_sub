"""Preview or persist immutable OSP KMZ source facts.

This command never writes canonical fiber/GIS asset tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal  # noqa: E402
from app.services.network.fiber_topology_staging import (  # noqa: E402
    SOURCE_PROFILES,
    preview_fiber_source,
    stage_fiber_source,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or stage normalized OSP fiber topology source facts."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profile", choices=sorted(SOURCE_PROFILES))
    selection.add_argument(
        "--all-checked-in",
        action="store_true",
        help="Process all six checked-in stable-ID OSP source profiles.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        help="Override the selected profile's checked-in source path.",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Persist immutable staging evidence; canonical assets are never written.",
    )
    parser.add_argument(
        "--actor",
        help="Required audit actor when --stage is used.",
    )
    parser.add_argument(
        "--include-features",
        action="store_true",
        help="Include every feature plan in preview JSON.",
    )
    return parser.parse_args()


def _selections(args: argparse.Namespace) -> list[tuple[str, Path]]:
    if args.all_checked_in:
        if args.path is not None:
            raise ValueError("--path cannot be combined with --all-checked-in")
        return [
            (name, PROJECT_ROOT / "docs" / profile.default_filename)
            for name, profile in sorted(SOURCE_PROFILES.items())
        ]
    assert args.profile is not None
    profile = SOURCE_PROFILES[args.profile]
    return [
        (
            args.profile,
            args.path or PROJECT_ROOT / "docs" / profile.default_filename,
        )
    ]


def main() -> int:
    args = parse_args()
    if args.stage and not (args.actor or "").strip():
        raise SystemExit("--actor is required with --stage")

    output: list[dict] = []
    blocked = False
    with SessionLocal() as db:
        for profile_name, path in _selections(args):
            if args.stage:
                result = stage_fiber_source(
                    db,
                    path,
                    profile_name,
                    created_by=args.actor,
                )
                payload = result.to_dict()
                payload["profile"] = profile_name
                payload["source_path"] = str(path)
                blocked = blocked or result.blocker_count > 0
            else:
                preview = preview_fiber_source(db, path, profile_name)
                payload = preview.to_dict(include_features=args.include_features)
                payload["source_path"] = str(path)
                blocked = blocked or preview.blocker_count > 0
            output.append(payload)

    print(json.dumps(output, indent=2, sort_keys=True))
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
