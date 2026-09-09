from pathlib import Path

from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_field_note_writer_has_one_typed_owner_contract() -> None:
    service = service_relationship("operations.field_notes")

    assert service.module == "app.services.field.note_commands"
    assert service.owns == ("native field work-order note creation",)
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "owner_managed"
    assert service.contract.events is not None
    assert service.contract.events.event_types == ("field_work_order_note.created",)


def test_field_note_adapter_cannot_restore_legacy_writer() -> None:
    route = (ROOT / "app/api/field/notes.py").read_text(encoding="utf-8")
    legacy = (ROOT / "app/services/field/notes.py").read_text(encoding="utf-8")

    assert "create_field_work_order_note(" in route
    assert "field_notes.create(" not in route
    assert "def create(" not in legacy


def test_mobile_note_delivery_keeps_stable_retry_and_visible_state() -> None:
    sync = (ROOT / "field_mobile/lib/core/offline/sync_service.dart").read_text(
        encoding="utf-8"
    )
    screen = (ROOT / "field_mobile/lib/features/jobs/job_detail_screen.dart").read_text(
        encoding="utf-8"
    )

    assert "entry.kind == 'note'" in sync
    assert "offlineNotesForJob" in sync
    assert "Note queued for sync" in screen
    assert "Note could not sync" in screen
