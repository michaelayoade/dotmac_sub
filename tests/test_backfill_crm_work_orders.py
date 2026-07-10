"""Phase 2 work-order backfill: note-import planning, provenance, idempotence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.models.field_note import FieldWorkOrderNote
from app.models.subscriber import Subscriber
from app.models.work_order_mirror import WorkOrderMirror
from scripts.migration.backfill_crm_work_orders import (
    ANONYMOUS_AUTHOR_KEY,
    is_crm_origin,
    is_open_status,
    note_marker,
    note_provenance,
    plan_note_imports,
    run,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


# ── Pure logic ─────────────────────────────────────────────────────────────


def test_is_open_status_terminal_vocabulary():
    assert is_open_status("scheduled") is True
    assert is_open_status("in_progress") is True
    assert is_open_status("completed") is False
    assert is_open_status("canceled") is False
    assert is_open_status(None) is True  # unknown -> treated open (safe side)


def test_is_crm_origin_excludes_native_rows():
    assert is_crm_origin("2f2b8f9e-aaaa-bbbb-cccc-121212121212") is True
    assert is_crm_origin("sub-abc123") is False
    assert is_crm_origin("") is False
    assert is_crm_origin(None) is False


def test_plan_note_imports_dedupes_on_marker_and_skips_unusable():
    notes = [
        {"id": "n1", "body": "First visit notes"},
        {"id": "n1", "body": "Duplicate delivery of n1"},
        {"id": "n2", "body": "Already imported"},
        {"id": "n3", "body": "   "},  # blank body — nothing to import
        {"body": "No id — no stable dedupe marker"},
        {"id": "n4", "body": "New note"},
    ]
    planned = plan_note_imports(notes, existing_markers={"n2"})
    assert [note_marker(n) for n in planned] == ["n1", "n4"]


def test_note_provenance_carries_crm_marker():
    note = {"id": "n9", "author_person_id": "p-1", "body": "x"}
    prov = note_provenance(note, now=NOW)
    assert prov == {
        "source": "crm",
        "crm_note_id": "n9",
        "crm_author_person_id": "p-1",
        "imported_at": NOW.isoformat(),
    }


# ── DB flow ────────────────────────────────────────────────────────────────


def _subscriber(db, crm_id=None) -> Subscriber:
    sub = Subscriber(
        first_name="Cust",
        last_name="Omer",
        email=f"c-{uuid.uuid4().hex[:8]}@example.com",
        crm_subscriber_id=crm_id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _mirror_row(db, sub, wo_id="wo-open-1", status="in_progress") -> WorkOrderMirror:
    row = WorkOrderMirror(
        subscriber_id=sub.id,
        crm_work_order_id=wo_id,
        title="Repair",
        status=status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _client(notes_by_wo):
    client = MagicMock()
    client.list_work_order_notes.side_effect = lambda wo_id: notes_by_wo.get(wo_id, [])
    return client


def test_live_run_imports_notes_with_provenance_and_is_idempotent(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    row = _mirror_row(db_session, sub)
    client = _client(
        {
            row.crm_work_order_id: [
                {
                    "id": "note-1",
                    "author_person_id": "crm-person-1",
                    "body": "Splice closure at pole 4",
                    "is_internal": True,
                    "created_at": "2026-07-01T09:00:00+00:00",
                },
                {
                    "id": "note-2",
                    "author_person_id": None,
                    "body": "Customer prefers afternoon",
                    "is_internal": False,
                    "created_at": "2026-07-02T10:00:00+00:00",
                },
            ]
        }
    )

    stats = run(db_session, client, dry_run=False, reconcile=False)
    assert stats["open_crm_work_orders"] == 1
    assert stats["notes_imported"] == 2
    assert stats["notes_skipped_existing"] == 0

    notes = (
        db_session.query(FieldWorkOrderNote)
        .filter_by(work_order_mirror_id=row.id)
        .order_by(FieldWorkOrderNote.created_at)
        .all()
    )
    assert len(notes) == 2
    assert notes[0].metadata_["source"] == "crm"
    assert notes[0].metadata_["crm_note_id"] == "note-1"
    assert notes[0].body == "Splice closure at pole 4"
    assert notes[0].is_internal is True
    assert notes[0].author_technician_id is not None
    assert notes[1].is_internal is False
    # Anonymous CRM note lands under the synthetic import author profile.
    anon = notes[1].author_technician
    assert anon.crm_person_id == ANONYMOUS_AUTHOR_KEY

    # Re-run: dedupe on the crm_note_id marker — nothing new.
    stats2 = run(db_session, client, dry_run=False, reconcile=False)
    assert stats2["notes_imported"] == 0
    assert stats2["notes_skipped_existing"] == 2
    assert (
        db_session.query(FieldWorkOrderNote)
        .filter_by(work_order_mirror_id=row.id)
        .count()
        == 2
    )


def test_dry_run_counts_without_writing(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    row = _mirror_row(db_session, sub)
    client = _client(
        {row.crm_work_order_id: [{"id": "note-1", "body": "Would import"}]}
    )

    with patch(
        "scripts.migration.backfill_crm_work_orders."
        "work_orders_mirror.reconcile_subscriber"
    ) as recon:
        stats = run(db_session, client, dry_run=True)

    recon.assert_not_called()  # dry-run never writes the mirror either
    assert stats["dry_run"] is True
    assert stats["linked_subscribers"] == 1
    assert stats["reconciled"] == 0
    assert stats["notes_imported"] == 1  # would-import count
    assert db_session.query(FieldWorkOrderNote).count() == 0


def test_terminal_and_native_rows_are_skipped_for_notes(db_session):
    sub = _subscriber(db_session, crm_id=uuid.uuid4())
    _mirror_row(db_session, sub, wo_id="wo-done", status="completed")
    _mirror_row(db_session, sub, wo_id="sub-native", status="in_progress")
    client = _client({})

    stats = run(db_session, client, dry_run=False, reconcile=False)

    assert stats["open_crm_work_orders"] == 0
    client.list_work_order_notes.assert_not_called()


def test_live_run_reconciles_linked_subscribers(db_session):
    _subscriber(db_session, crm_id=uuid.uuid4())
    _subscriber(db_session, crm_id=uuid.uuid4())
    _subscriber(db_session)  # not CRM-linked — never reconciled
    client = _client({})

    with patch(
        "scripts.migration.backfill_crm_work_orders."
        "work_orders_mirror.reconcile_subscriber",
        return_value=True,
    ) as recon:
        stats = run(db_session, client, dry_run=False, notes=False)

    assert stats["linked_subscribers"] == 2
    assert stats["reconciled"] == 2
    assert recon.call_count == 2
