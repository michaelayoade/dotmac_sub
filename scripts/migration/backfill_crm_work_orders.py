#!/usr/bin/env python3
"""Phase 2 work-order backfill — flip prep (12-phase2-completion.md §A5).

One-shot, READ ONLY on the CRM:

1. **Reconcile-all** — pull work orders for EVERY CRM-linked subscriber into
   the local mirror through the existing reconcile machinery
   (``work_orders_mirror.reconcile_subscriber``), regardless of sync-state
   staleness, so the mirror is complete before the ``crm.work_order_pull``
   flip.
2. **Notes** — import OPEN (non-terminal) CRM-origin work orders'
   ``work_order_notes`` into ``field_work_order_notes`` for tech continuity.
   Each imported note carries provenance metadata
   ``{"source": "crm", "crm_note_id": ...}``; the ``crm_note_id`` marker is
   the dedupe key, so re-runs are idempotent. Completed/canceled work orders'
   notes stay frozen in the CRM (archive posture — never mirrored).

Dry-run by default: NO sub writes at all. Reconcile is skipped (it writes the
mirror) and notes are only counted, so the dry-run note counts cover work
orders already mirrored. Run --live once, re-run safely as needed.

Uses the app's own DB session + CRM client — run on a host with app config.

Usage:
    python -m scripts.migration.backfill_crm_work_orders             # dry-run
    python -m scripts.migration.backfill_crm_work_orders --live
    python -m scripts.migration.backfill_crm_work_orders --live --skip-notes
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.models.dispatch import TechnicianProfile  # noqa: E402
from app.models.field_note import FieldWorkOrderNote  # noqa: E402
from app.models.subscriber import Subscriber  # noqa: E402
from app.models.work_order_mirror import WorkOrderMirror  # noqa: E402
from app.services import work_orders_mirror  # noqa: E402
from app.services.crm_client import CRMClientError  # noqa: E402

logger = logging.getLogger("backfill_crm_work_orders")

# Vocabulary is shared 1:1 with the CRM WorkOrderStatus enum.
TERMINAL_STATUSES = frozenset({"completed", "canceled"})

# Technician-profile key for CRM notes with no author (author_person_id NULL).
ANONYMOUS_AUTHOR_KEY = "crm-note-import"


def is_open_status(status: str | None) -> bool:
    return (status or "").strip().lower() not in TERMINAL_STATUSES


def is_crm_origin(crm_work_order_id: str | None) -> bool:
    """Native rows are born with a ``sub-`` public id — they have no CRM notes."""
    value = str(crm_work_order_id or "")
    return bool(value) and not value.startswith("sub-")


def note_marker(note: dict[str, Any]) -> str | None:
    """The CRM note id — the provenance/dedupe marker."""
    value = str(note.get("id") or "").strip()
    return value or None


def note_provenance(note: dict[str, Any], *, now: datetime | None = None) -> dict:
    return {
        "source": "crm",
        "crm_note_id": note_marker(note),
        "crm_author_person_id": str(note.get("author_person_id") or "") or None,
        "imported_at": (now or datetime.now(UTC)).isoformat(),
    }


def plan_note_imports(
    notes: list[dict[str, Any]], existing_markers: set[str]
) -> list[dict[str, Any]]:
    """Notes not yet imported (dedupe on the crm_note_id marker); notes without
    an id are skipped — there is nothing stable to dedupe on."""
    planned: list[dict[str, Any]] = []
    seen: set[str] = set(existing_markers)
    for note in notes:
        marker = note_marker(note)
        if marker is None or marker in seen:
            continue
        if not str(note.get("body") or "").strip():
            continue
        seen.add(marker)
        planned.append(note)
    return planned


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _existing_markers(db: Session, mirror_id) -> set[str]:
    markers: set[str] = set()
    rows = db.scalars(
        select(FieldWorkOrderNote.metadata_).where(
            FieldWorkOrderNote.work_order_mirror_id == mirror_id
        )
    ).all()
    for metadata in rows:
        if isinstance(metadata, dict):
            marker = str(metadata.get("crm_note_id") or "").strip()
            if marker:
                markers.add(marker)
    return markers


def _author_profile(db: Session, crm_person_id: str | None) -> TechnicianProfile:
    key = str(crm_person_id or "").strip() or ANONYMOUS_AUTHOR_KEY
    # Reuse the mirror's profile-ensure (uuid5 person identity, metadata stamp).
    work_orders_mirror._ensure_technician_profile(db, crm_person_id=key)
    db.flush()
    profile = db.scalar(
        select(TechnicianProfile).where(TechnicianProfile.crm_person_id == key)
    )
    assert profile is not None  # _ensure_technician_profile just added it
    return profile


def _import_notes_for_row(
    db: Session,
    client,
    row: WorkOrderMirror,
    *,
    dry_run: bool,
    stats: dict[str, int],
) -> None:
    try:
        notes = client.list_work_order_notes(row.crm_work_order_id) or []
    except CRMClientError as exc:
        stats["note_errors"] += 1
        logger.warning(
            "note_list_failed work_order_id=%s: %s", row.crm_work_order_id, exc
        )
        return
    stats["notes_seen"] += len(notes)
    if not notes:
        return
    existing = _existing_markers(db, row.id)
    planned = plan_note_imports(notes, existing)
    stats["notes_skipped_existing"] += len(notes) - len(planned)
    stats["notes_imported"] += len(planned)
    if dry_run or not planned:
        return
    now = datetime.now(UTC)
    for note in planned:
        profile = _author_profile(db, note.get("author_person_id"))
        db.add(
            FieldWorkOrderNote(
                id=uuid.uuid4(),
                work_order_mirror_id=row.id,
                crm_work_order_id=row.crm_work_order_id,
                author_technician_id=profile.id,
                author_person_id=profile.person_id,
                author_name=(profile.metadata_ or {}).get("name"),
                body=str(note.get("body") or "").strip(),
                is_internal=bool(note.get("is_internal", True)),
                created_at=_parse_dt(note.get("created_at")) or now,
                metadata_=note_provenance(note, now=now),
            )
        )
    db.commit()


def run(
    db: Session,
    client,
    *,
    dry_run: bool = True,
    reconcile: bool = True,
    notes: bool = True,
    limit: int | None = None,
) -> dict[str, int | bool]:
    stats: dict[str, int | bool] = {
        "dry_run": dry_run,
        "linked_subscribers": 0,
        "reconciled": 0,
        "reconcile_errors": 0,
        "open_crm_work_orders": 0,
        "notes_seen": 0,
        "notes_imported": 0,
        "notes_skipped_existing": 0,
        "note_errors": 0,
    }

    linked_ids = db.scalars(
        select(Subscriber.id)
        .where(Subscriber.crm_subscriber_id.isnot(None))
        .order_by(Subscriber.id)
    ).all()
    if limit is not None:
        linked_ids = linked_ids[: max(0, limit)]
    stats["linked_subscribers"] = len(linked_ids)

    if reconcile and not dry_run:
        for subscriber_id in linked_ids:
            try:
                if work_orders_mirror.reconcile_subscriber(db, str(subscriber_id)):
                    stats["reconciled"] += 1  # type: ignore[operator]
            except CRMClientError as exc:
                db.rollback()
                stats["reconcile_errors"] += 1  # type: ignore[operator]
                logger.warning("reconcile_failed subscriber=%s: %s", subscriber_id, exc)

    if notes:
        open_rows = [
            row
            for row in db.scalars(
                select(WorkOrderMirror).where(WorkOrderMirror.is_active.is_(True))
            ).all()
            if is_crm_origin(row.crm_work_order_id) and is_open_status(row.status)
        ]
        stats["open_crm_work_orders"] = len(open_rows)
        note_stats: dict[str, int] = {
            "notes_seen": 0,
            "notes_imported": 0,
            "notes_skipped_existing": 0,
            "note_errors": 0,
        }
        for row in open_rows:
            _import_notes_for_row(db, client, row, dry_run=dry_run, stats=note_stats)
        stats.update(note_stats)

    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="write to sub (default is a no-write dry run)",
    )
    parser.add_argument(
        "--skip-reconcile", action="store_true", help="skip the reconcile-all pass"
    )
    parser.add_argument(
        "--skip-notes", action="store_true", help="skip the notes import pass"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="max CRM-linked subscribers"
    )
    args = parser.parse_args()

    from app.db import SessionLocal
    from app.services.crm_client import get_crm_client

    db = SessionLocal()
    try:
        stats = run(
            db,
            get_crm_client(),
            dry_run=not args.live,
            reconcile=not args.skip_reconcile,
            notes=not args.skip_notes,
            limit=args.limit,
        )
    finally:
        db.close()
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
