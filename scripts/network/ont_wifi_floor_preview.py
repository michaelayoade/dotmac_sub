"""Read-only preview: how many ONTs would the Wi-Fi security floor change?

Uses stored desired state and the last stored ACS observation only. Opens no
writes and performs no device I/O.

    poetry run python -m scripts.network.ont_wifi_floor_preview
"""

from __future__ import annotations

import json

from app.db import SessionLocal
from app.services.network.reconcile.wifi_floor_preview import (
    preview_wifi_security_floor,
)


def main() -> int:
    with SessionLocal() as db:
        preview = preview_wifi_security_floor(db)
        db.rollback()
    print(
        json.dumps(
            {
                "examined": preview.examined,
                "would_change": preview.would_change,
                "needs_psk_skipped": preview.needs_psk_skipped,
                "already_meets_floor_or_unmanaged": (
                    preview.already_meets_floor_or_unmanaged
                ),
                "no_observation": preview.no_observation,
                "unreadable": preview.unreadable,
            },
            indent=2,
        )
    )
    return 1 if preview.unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
