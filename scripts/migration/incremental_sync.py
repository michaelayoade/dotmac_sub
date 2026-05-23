"""Compatibility wrapper for the migrated incremental sync module."""

from __future__ import annotations

import sys

from app.services.migrations.incremental_sync import *  # noqa: F401,F403
from app.services.migrations.incremental_sync import (  # noqa: F401
    _is_splynx_deleted,
    _parse_date,
    _payment_since_expression,
    run_incremental_sync,
)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    hours = 24
    for arg in args:
        if arg.startswith("--hours="):
            hours = int(arg.split("=", 1)[1])

    if "--execute" in args:
        run_incremental_sync(hours_back=hours, dry_run=False)
    else:
        run_incremental_sync(hours_back=hours, dry_run=True)
        print(
            "\nTo execute: poetry run python -m scripts.migration.incremental_sync --execute"
        )
        print("Options: --hours=48 (default: 24)")


if __name__ == "__main__":
    main()
