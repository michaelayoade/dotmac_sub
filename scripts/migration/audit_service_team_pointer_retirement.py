"""Read-only aggregate gate for migration-426 service-team pointers."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.service_team_pointer_retirement import (
    audit_service_team_pointer_retirement,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        summary = audit_service_team_pointer_retirement(db).summary()
        db.rollback()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 2 if args.check and not summary["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
