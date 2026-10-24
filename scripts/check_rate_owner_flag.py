"""Read-only pre-deploy check: is the rate owner switched on in this database?

Run as ``python -m scripts.check_rate_owner_flag`` from the image's working
directory: the app is not an installed package, so ``-m`` is what puts the
repository root on ``sys.path`` (a plain ``python scripts/...`` run would only
add ``scripts/`` and fail to import ``app``).

Exit 0 only when the stored flag is true. Exit 3 when it is off or has never
been written. Exit 4 when it cannot be read, including an import failure; only
the exception class is printed, never a connection detail. Reads only the two
value columns of the one settings row, so a later column cannot break it.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.services.access_rate_profile import SETTING_KEY

        with SessionLocal() as db:
            row = db.execute(
                select(DomainSetting.value_json, DomainSetting.value_text)
                .where(DomainSetting.domain == SettingDomain.radius)
                .where(DomainSetting.key == SETTING_KEY)
                .where(DomainSetting.is_active.is_(True))
            ).first()
            db.rollback()
    except Exception as exc:  # noqa: BLE001 - report the class only
        print(f"rate owner flag unreadable: {type(exc).__name__}", file=sys.stderr)
        return 4
    if row is None:
        print("radius.rate_owner_enabled=unset")
        return 3
    value = row.value_json if row.value_json is not None else row.value_text
    flag = str(value).strip().lower() in {"true", "1"}
    print(f"radius.rate_owner_enabled={flag}")
    return 0 if flag else 3


if __name__ == "__main__":
    raise SystemExit(main())
