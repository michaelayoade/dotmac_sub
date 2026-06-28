#!/usr/bin/env python3
"""Deactivate dormant ONT inventory left behind by the 2026-03-16 migration import.

Background
----------
A large cohort of ONT rows was bulk-imported (``provisioning_status`` NULL,
``is_active`` true) during the legacy->local migration but never run through
TR-069 bootstrap, so they have no active GenieACS binding
(``tr069_cpe_devices.genieacs_device_id``). Their last TR-069 snapshot is frozen
in the past; nothing refreshes them, and the metrics pusher used to re-emit those
frozen samples (see ``app/services/network/olt_polling_metrics.py`` — now
ingest-time stamped + ACS-link scoped). The vast majority are physically
OLT-offline / never-seen devices whose subscriber "active" flag is stale.

This CLI deactivates the *safe* dormant rows so they stop inflating active-ONT
counts, dashboards and metric exports. It is deliberately conservative.

A row is a deactivation candidate only when ALL of:
  * ``ont_units.is_active`` is true
  * it has NO active GenieACS link (no active ``tr069_cpe_devices`` row with a
    non-null ``genieacs_device_id``)
  * ``olt_status`` is not 'online'
  * it has not been seen online at the OLT within ``--offline-days`` (or never)

and it is NOT protected by the RADIUS safety gate. A row is PROTECTED (kept,
never touched) when its assigned subscriber has a live RADIUS session, or any
accounting session updated within ``--radius-days`` — i.e. the customer is
actually online via PPPoE regardless of what the ONT/OLT side reports.

Rows with no active subscriber assignment ("orphans") are candidates (there is
no customer to protect).

Deactivation = set ``is_active = False`` and append an audit line to ``notes``.
It is reversible (flip ``is_active`` back). Active ``ont_assignments`` are left
intact. Dry-run by default; nothing is written without --apply.

Examples
--------
  # Review what would be deactivated and what is protected (read-only):
  python -m scripts.one_off.deactivate_dormant_onts

  # Write a full CSV review report alongside the dry run:
  python -m scripts.one_off.deactivate_dormant_onts --csv /tmp/dormant_onts.csv

  # Apply (deactivate the safe cohort):
  python -m scripts.one_off.deactivate_dormant_onts --apply

  # Be stricter about staleness (only ONTs offline for 60+ days):
  python -m scripts.one_off.deactivate_dormant_onts --offline-days 60
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.catalog import Subscription
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.models.tr069 import Tr069CpeDevice
from app.models.usage import RadiusAccountingSession


@dataclass
class Candidate:
    ont: OntUnit
    olt_name: str
    subscriber_id: object
    subscriber: Subscriber | None
    bucket: str  # 'orphan_no_subscriber' | 'dormant_no_radius'


def _active_acs_linked_ont_ids(db: Session) -> set:
    rows = db.execute(
        select(Tr069CpeDevice.ont_unit_id).where(
            Tr069CpeDevice.is_active.is_(True),
            Tr069CpeDevice.genieacs_device_id.is_not(None),
            Tr069CpeDevice.ont_unit_id.is_not(None),
        )
    ).all()
    return {r[0] for r in rows}


def _subscribers_with_recent_radius(db: Session, radius_cutoff: datetime) -> set:
    """Subscriber ids with a live session or accounting session within cutoff."""
    protected: set = set()

    live = db.execute(
        select(RadiusActiveSession.subscriber_id).where(
            RadiusActiveSession.subscriber_id.is_not(None)
        )
    ).all()
    protected.update(r[0] for r in live)

    # Accounting sessions only carry subscription_id -> map to subscriber_id.
    recency = func.coalesce(
        RadiusAccountingSession.last_update_at,
        RadiusAccountingSession.session_end,
        RadiusAccountingSession.session_start,
    )
    acct = db.execute(
        select(Subscription.subscriber_id)
        .join(
            RadiusAccountingSession,
            RadiusAccountingSession.subscription_id == Subscription.id,
        )
        .where(
            Subscription.subscriber_id.is_not(None),
            recency >= radius_cutoff,
        )
    ).all()
    protected.update(r[0] for r in acct)

    protected.discard(None)
    return protected


def collect_candidates(
    db: Session, offline_days: int, radius_days: int
) -> tuple[list[Candidate], int]:
    now = datetime.now(UTC)
    offline_cutoff = now - timedelta(days=offline_days)
    radius_cutoff = now - timedelta(days=radius_days)

    acs_linked = _active_acs_linked_ont_ids(db)
    protected_subs = _subscribers_with_recent_radius(db, radius_cutoff)

    # Active, OLT-offline, not-recently-seen ONTs.
    stmt = (
        select(OntUnit, OLTDevice.name)
        .outerjoin(OLTDevice, OLTDevice.id == OntUnit.olt_device_id)
        .where(
            OntUnit.is_active.is_(True),
            OntUnit.olt_status != "online",
            (OntUnit.last_seen_at.is_(None)) | (OntUnit.last_seen_at < offline_cutoff),
        )
    )
    rows = db.execute(stmt).all()

    candidates: list[Candidate] = []
    protected_count = 0
    for ont, olt_name in rows:
        if ont.id in acs_linked:
            continue  # still ACS-managed; not dormant

        assignment = (
            db.execute(
                select(OntAssignment).where(
                    OntAssignment.ont_unit_id == ont.id,
                    OntAssignment.active.is_(True),
                )
            )
            .scalars()
            .first()
        )

        subscriber_id = assignment.subscriber_id if assignment else None

        if subscriber_id is None:
            bucket = "orphan_no_subscriber"
        elif subscriber_id in protected_subs:
            protected_count += 1
            continue  # customer is online -> never touch
        else:
            bucket = "dormant_no_radius"

        subscriber = (
            db.get(Subscriber, subscriber_id) if subscriber_id is not None else None
        )
        candidates.append(
            Candidate(
                ont=ont,
                olt_name=olt_name or "-",
                subscriber_id=subscriber_id,
                subscriber=subscriber,
                bucket=bucket,
            )
        )

    return candidates, protected_count


def write_csv(path: str, candidates: list[Candidate]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "bucket",
                "ont_serial",
                "olt_name",
                "ont_name",
                "olt_status",
                "last_seen_at",
                "last_snapshot_at",
                "subscriber",
                "subscriber_status",
                "subscriber_email",
            ]
        )
        for c in candidates:
            s = c.subscriber
            w.writerow(
                [
                    c.bucket,
                    c.ont.serial_number,
                    c.olt_name,
                    c.ont.name or "",
                    c.ont.olt_status,
                    c.ont.last_seen_at.isoformat() if c.ont.last_seen_at else "",
                    c.ont.tr069_last_snapshot_at.isoformat()
                    if c.ont.tr069_last_snapshot_at
                    else "",
                    (s.display_name if s else "") or "",
                    (str(s.status) if s else "") or "",
                    (s.email if s else "") or "",
                ]
            )


def apply_deactivation(db: Session, candidates: list[Candidate], stamp: str) -> int:
    changed = 0
    note = f"[{stamp}] deactivated by deactivate_dormant_onts (dormant migration import, no ACS link, OLT-offline, no recent RADIUS)"
    for c in candidates:
        ont = c.ont
        ont.is_active = False
        existing = (ont.notes or "").rstrip()
        ont.notes = f"{existing}\n{note}".strip() if existing else note
        changed += 1
    if changed:
        db.commit()
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry-run)"
    )
    parser.add_argument(
        "--offline-days",
        type=int,
        default=30,
        help="ONT must not have been seen online at the OLT within this many days (default 30)",
    )
    parser.add_argument(
        "--radius-days",
        type=int,
        default=30,
        help="Protect subscribers with a RADIUS accounting session within this many days (default 30)",
    )
    parser.add_argument("--csv", help="Write a full review CSV to this path")
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        candidates, protected = collect_candidates(
            db, args.offline_days, args.radius_days
        )

        orphans = [c for c in candidates if c.bucket == "orphan_no_subscriber"]
        dormant = [c for c in candidates if c.bucket == "dormant_no_radius"]

        print("=" * 64)
        print("Dormant ONT deactivation " + ("(APPLY)" if args.apply else "(DRY-RUN)"))
        print("=" * 64)
        print(f"offline-days={args.offline_days}  radius-days={args.radius_days}")
        print(f"  candidates to deactivate : {len(candidates)}")
        print(f"      orphan_no_subscriber : {len(orphans)}")
        print(f"      dormant_no_radius    : {len(dormant)}")
        print(f"  protected (online RADIUS): {protected}  <- KEPT, never touched")

        if args.csv:
            write_csv(args.csv, candidates)
            print(f"  review CSV written        : {args.csv}")

        if args.apply:
            changed = apply_deactivation(db, candidates, stamp)
            print(f"\nAPPLIED: deactivated {changed} ONT rows (is_active=False).")
        else:
            print("\nDry-run only. Re-run with --apply to deactivate.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
