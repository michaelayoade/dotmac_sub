"""Converge plaintext credential-at-rest values through the shared inventory.

Dry-run is the default. Output is aggregate JSON and never contains credential
values, record identifiers, names, hostnames, or serial numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from app.db import SessionLocal
from app.services.credential_key_rotation import remediate_credential_encryption


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report aggregate remediation counts without changing credentials",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Encrypt all plaintext values found by the shared inventory",
    )
    mode.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = _parser().parse_args(argv)
    execute = bool(args.execute or args.apply)
    db = SessionLocal()
    try:
        try:
            result = remediate_credential_encryption(db, execute=execute)
        except Exception as exc:
            db.rollback()
            print(
                json.dumps(
                    {
                        "status": "error",
                        "execute": execute,
                        "error_type": type(exc).__name__,
                        "error": "Credential remediation failed.",
                    }
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(result.as_dict(), sort_keys=True))
        if result.status == "blocked":
            return 2
        if result.status == "incomplete":
            return 3
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
