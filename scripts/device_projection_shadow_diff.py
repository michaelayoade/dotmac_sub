"""Shadow diff: materialised ``device_projections`` vs live ``collect_devices``.

The cutover gate for the device-list authority migration (see
``app/services/device_projection_reconcile.py``). With the reconciler
populating ``device_projections`` in prod, this compares each projected row
against a fresh live derivation to quantify:

  * count parity      - rows present in each
  * membership drift  - live-only = insert lag; projection-only = prune lag
  * status agreement  - operational_status matches the live-derived status
  * freshness         - age of the oldest / newest refreshed_at

Run it after Phase A deploys to turn the reconcile-interval / staleness
decision into evidence *before* cutting the list read over to the projection:
low membership drift + rare status mismatch => a longer interval is safe;
frequent status churn => shorten the interval or keep detail views live.

Read-only. Opens no writes. ``--json`` emits the raw diff; ``--strict`` exits
non-zero when any status mismatch or membership drift exists (usable as a gate).

Run from the repo root as a module::

    poetry run python -m scripts.device_projection_shadow_diff [--json] [--strict]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network_monitoring import DeviceProjection
from app.services.web_network_core_devices_inventory import collect_devices


def _key(device_type: object, source_id: object) -> tuple[str, str]:
    return (str(device_type), str(source_id))


def _age_seconds(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    # Postgres returns tz-aware; SQLite drops tzinfo. Normalise to UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (now - value).total_seconds()


def compute_diff(db) -> dict:
    """Compare the projection against a fresh live derivation."""
    live = {
        _key(d["type"], d["id"]): str(d.get("status") or "unknown")
        for d in collect_devices(db)
    }
    projected_rows = list(db.execute(select(DeviceProjection)).scalars())
    projected = {_key(r.device_type, r.source_id): r for r in projected_rows}

    both = set(live) & set(projected)
    live_only = sorted(set(live) - set(projected))
    proj_only = sorted(set(projected) - set(live))
    status_mismatch = [
        {
            "device_type": k[0],
            "source_id": k[1],
            "live": live[k],
            "projected": projected[k].operational_status,
        }
        for k in sorted(both)
        if live[k] != projected[k].operational_status
    ]

    now = datetime.now(UTC)
    ages = [
        age
        for r in projected_rows
        if (age := _age_seconds(r.refreshed_at, now)) is not None
    ]
    return {
        "live_total": len(live),
        "projection_total": len(projected),
        "in_both": len(both),
        "live_only": [{"device_type": k[0], "source_id": k[1]} for k in live_only],
        "projection_only": [
            {"device_type": k[0], "source_id": k[1]} for k in proj_only
        ],
        "status_mismatch": status_mismatch,
        "oldest_refresh_age_seconds": max(ages) if ages else None,
        "newest_refresh_age_seconds": min(ages) if ages else None,
    }


def _print_human(diff: dict) -> None:
    print("device_projection shadow diff")
    print(f"  live devices        : {diff['live_total']}")
    print(f"  projection rows     : {diff['projection_total']}")
    print(f"  present in both     : {diff['in_both']}")
    print(f"  live-only (insert lag)   : {len(diff['live_only'])}")
    print(f"  projection-only (prune lag): {len(diff['projection_only'])}")
    print(f"  status mismatches   : {len(diff['status_mismatch'])}")
    oldest = diff["oldest_refresh_age_seconds"]
    newest = diff["newest_refresh_age_seconds"]
    if oldest is not None:
        print(f"  refresh age (s)     : oldest={oldest:.0f} newest={newest:.0f}")
    for m in diff["status_mismatch"][:20]:
        print(
            f"    ! {m['device_type']}:{m['source_id']} "
            f"live={m['live']} projected={m['projected']}"
        )
    if len(diff["status_mismatch"]) > 20:
        print(f"    ... and {len(diff['status_mismatch']) - 20} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit raw diff as JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on any status mismatch or membership drift",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        diff = compute_diff(db)

    if args.json:
        print(json.dumps(diff, indent=2, default=str))
    else:
        _print_human(diff)

    if args.strict and (
        diff["status_mismatch"] or diff["live_only"] or diff["projection_only"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
