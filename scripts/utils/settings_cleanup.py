"""Report or deactivate retired no-op settings."""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.services.settings_health import deactivate_retired_settings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deactivate retired settings that have no runtime consumer.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist soft-deactivation; the default is a dry run.",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    db = SessionLocal()
    try:
        identities = deactivate_retired_settings(db, apply=args.apply)
    finally:
        db.close()

    action = "deactivated" if args.apply else "would deactivate"
    print(f"Retired settings {action}: {len(identities)}")
    for identity in identities:
        print(f"- {identity}")


if __name__ == "__main__":
    main()
