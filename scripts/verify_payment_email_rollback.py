"""Read-only candidate-image gate before restoring an older application image.

Exit 0 means the database still has the proved pre-installation legacy shape.
Every other result forbids automatic image rollback. This script never writes.
"""

from __future__ import annotations

import sys

from app.db import SessionLocal, begin_read_only_snapshot
from app.services.payment_email_cutover import (
    legacy_image_rollback_allowed,
    read_legacy_image_rollback_floor,
)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("PAYMENT EMAIL ROLLBACK REFUSED: unexpected arguments", file=sys.stderr)
        return 2
    try:
        with SessionLocal() as db:
            begin_read_only_snapshot(db)
            observation = read_legacy_image_rollback_floor(db)
            allowed = legacy_image_rollback_allowed(observation)
            db.rollback()
    except Exception:
        # Deliberately omit driver messages: they may contain a connection URL.
        print(
            "PAYMENT EMAIL ROLLBACK REFUSED: catalog proof unavailable", file=sys.stderr
        )
        return 2
    if not allowed:
        print(
            "PAYMENT EMAIL ROLLBACK REFUSED: installed composition floor",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
