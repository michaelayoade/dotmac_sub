"""Plan or execute lifecycle-safe cleanup of unrecoverable credentials."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.db import SessionLocal
from app.services.credential_lifecycle_cleanup import (
    cleanup_unrecoverable_credentials,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--confirm-plan-digest",
        help="Exact digest emitted by the reviewed dry-run plan",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = _parser().parse_args(argv)
    db = SessionLocal()
    try:
        try:
            result = cleanup_unrecoverable_credentials(
                db,
                execute=args.execute,
                confirm_plan_digest=args.confirm_plan_digest,
            )
        except Exception as exc:
            db.rollback()
            print(
                json.dumps(
                    {
                        "status": "error",
                        "execute": args.execute,
                        "error_type": type(exc).__name__,
                        "error": "Credential lifecycle cleanup failed.",
                    }
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result.as_dict(), sort_keys=True))
        if result.status in {"blocked", "confirmation_required"}:
            return 2
        if result.status == "incomplete":
            return 3
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
