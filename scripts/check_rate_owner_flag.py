"""Read-only pre-deploy check: is the rate owner switched on in this database?

Exit 0 only when the stored flag is true. Exit 3 when it is off or has never
been written, 4 when it cannot be read. Uses the same uncached read as the
governed switch; writes nothing and prints no connection details.
"""

from __future__ import annotations

import sys


def main() -> int:
    from app.db import SessionLocal
    from app.services import access_rate_switch

    try:
        with SessionLocal() as db:
            flag = access_rate_switch.flag_from_row(db)
            db.rollback()
    except Exception as exc:  # noqa: BLE001 - report the class only
        print(f"rate owner flag unreadable: {type(exc).__name__}", file=sys.stderr)
        return 4
    print(f"radius.rate_owner_enabled={flag}")
    return 0 if flag is True else 3


if __name__ == "__main__":
    raise SystemExit(main())
