"""Fast unit evidence for the native vendor field execution boundary."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_job_event import FieldJobEvent
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.field_worklog import FieldWorkLog
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    FieldEvent,
    FieldJobQuery,
    FieldJobsQuery,
    FieldWorkLogEntry,
    SubmitFieldWorkLogs,
)
from app.services.field.jobs import field_jobs
from app.services.field.note_commands import (
    CreateFieldWorkOrderNote,
    create_field_work_order_note,
)
from app.services.field.schedule import field_schedule
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import (
    FieldAccessError,
    FieldActorKind,
    ResolveFieldActor,
    resolve_field_actor,
)
from app.services.field.worklogs import field_worklogs
from app.services.owner_commands import CommandContext


def _vendor(db: Session) -> tuple[SystemUser, FieldVendorUser, Vendor]:
    user = SystemUser(
        first_name="Vendor", last_name="Crew", email=f"{uuid4()}@example.com"
    )
    native = Vendor(name="Contractor")
    db.add_all([user, native])
    db.flush()
    field_vendor = FieldVendor(name="Contractor", crm_vendor_id=str(native.id))
    db.add(field_vendor)
    db.flush()
    membership = FieldVendorUser(
        vendor_id=field_vendor.id, system_user_id=user.id, role="field"
    )
    db.add(membership)
    db.flush()
    return user, membership, native


def _job(
    db: Session, vendor: Vendor, *, metadata: dict[str, str] | None = None
) -> WorkOrder:
    job = WorkOrder(
        public_id=f"wo-{uuid4()}",
        title="Site visit",
        status="scheduled",
        metadata_=metadata,
        scheduled_start=datetime.now(UTC),
    )
    db.add(job)
    db.flush()
    db.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=job.id, assigned_vendor_id=vendor.id, status="assigned"
        )
    )
    db.flush()
    return job


def _context(user: SystemUser) -> CommandContext:
    return CommandContext.system(
        actor=str(user.id),
        scope="field",
        reason="Vendor field execution test",
        idempotency_key=str(uuid4()),
    )


def test_vendor_reads_native_assignment_without_technician(db_session: Session) -> None:
    user, membership, native = _vendor(db_session)
    other_user, _, other_native = _vendor(db_session)
    job = _job(db_session, native)
    hidden = _job(
        db_session,
        other_native,
        metadata={"assigned_vendor_id": str(membership.vendor_id)},
    )
    db_session.commit()
    actor = resolve_field_actor(db_session, ResolveFieldActor(user.id))
    assert actor.kind == FieldActorKind.vendor
    assert actor.technician_id is None and actor.person_id is None
    assert [
        item.id for item in field_jobs.list(db_session, FieldJobsQuery(user.id))
    ] == [job.public_id]
    assert field_jobs.me(db_session, ResolveFieldActor(user.id)).person_id is None
    assert not field_jobs.me(
        db_session, ResolveFieldActor(user.id)
    ).capabilities.attendance.available
    detail = field_jobs.get_detail(db_session, FieldJobQuery(user.id, job.public_id))
    assert detail.job.id == job.public_id
    assert detail.materials == [] and detail.equipment is None
    assert [
        item.reference_id
        for item in field_schedule.timeline(db_session, FieldJobsQuery(user.id))
    ] == [job.public_id]
    with pytest.raises(FieldAccessError):
        field_jobs.get_detail(db_session, FieldJobQuery(user.id, hidden.public_id))
    assert other_user.id != user.id


def test_vendor_with_stale_technician_never_gets_staff_scope(
    db_session: Session,
) -> None:
    user, _, native = _vendor(db_session)
    technician = TechnicianProfile(
        person_id=user.id, system_user_id=user.id, crm_person_id=f"old-{uuid4()}"
    )
    db_session.add(technician)
    db_session.flush()
    hidden = WorkOrder(
        public_id=f"wo-{uuid4()}",
        title="Staff job",
        assigned_to_crm_person_id=technician.crm_person_id,
    )
    db_session.add(hidden)
    assigned = _job(db_session, native)
    db_session.commit()
    assert [
        item.id for item in field_jobs.list(db_session, FieldJobsQuery(user.id))
    ] == [assigned.public_id]
    with pytest.raises(FieldAccessError):
        field_jobs.get_detail(db_session, FieldJobQuery(user.id, hidden.public_id))


def test_disabled_and_ambiguous_vendor_memberships_fail_closed(
    db_session: Session,
) -> None:
    user, membership, _ = _vendor(db_session)
    membership.is_active = False
    db_session.commit()
    with pytest.raises(FieldAccessError):
        resolve_field_actor(db_session, ResolveFieldActor(user.id))
    membership.is_active = True
    other = FieldVendor(name="Second contractor", crm_vendor_id=str(uuid4()))
    db_session.add(other)
    db_session.flush()
    db_session.add(
        FieldVendorUser(vendor_id=other.id, system_user_id=user.id, role="field")
    )
    db_session.commit()
    with pytest.raises(FieldAccessError):
        resolve_field_actor(db_session, ResolveFieldActor(user.id))


def test_vendor_start_pause_resume_completion_and_scoped_replay(
    db_session: Session,
) -> None:
    user, membership, native = _vendor(db_session)
    job = _job(db_session, native)
    user_id, membership_id, public_id = user.id, membership.id, job.public_id
    context = _context(user)
    db_session.commit()
    start = ApplyFieldTransition(context, user_id, public_id, FieldEvent.start, uuid4())
    result = field_transitions.apply(db_session, start)
    assert result.job.status == "in_progress" and not result.replayed
    # Queries open a read transaction; the adapter closes it before commands.
    assert db_session.query(FieldWorkLog).one().author_vendor_user_id == membership_id
    db_session.rollback()
    assert field_transitions.apply(db_session, start).replayed
    with pytest.raises(FieldAccessError, match="different details"):
        field_transitions.apply(db_session, replace(start, note="changed"))
    field_transitions.apply(
        db_session, replace(start, client_event_id=uuid4(), event=FieldEvent.pause)
    )
    field_transitions.apply(
        db_session, replace(start, client_event_id=uuid4(), event=FieldEvent.resume)
    )
    with pytest.raises(FieldAccessError, match="photo"):
        field_transitions.apply(
            db_session,
            replace(start, client_event_id=uuid4(), event=FieldEvent.complete),
        )
    other_user, _, other_native = _vendor(db_session)
    queue = (
        db_session.query(WorkOrderAssignmentQueue)
        .filter(WorkOrderAssignmentQueue.work_order_mirror_id == job.id)
        .one()
    )
    queue.assigned_vendor_id = other_native.id
    db_session.commit()
    with pytest.raises(FieldAccessError):
        field_transitions.apply(db_session, start)
    assert other_user.id != user_id


def test_vendor_notes_and_manual_worklogs_use_explicit_actor(
    db_session: Session,
) -> None:
    user, membership, native = _vendor(db_session)
    job = _job(db_session, native)
    user_id, membership_id, public_id = user.id, membership.id, job.public_id
    context = _context(user)
    note = CreateFieldWorkOrderNote(
        context, user_id, public_id, uuid4(), "Vendor work note", True
    )
    db_session.commit()
    saved = create_field_work_order_note(db_session, note)
    assert saved.author_person_id is None
    assert create_field_work_order_note(db_session, note).replayed
    command = SubmitFieldWorkLogs(
        context,
        user_id,
        public_id,
        (
            FieldWorkLogEntry(
                datetime.now(UTC) - timedelta(hours=2),
                datetime.now(UTC) - timedelta(hours=1),
                "Manual",
                uuid4(),
            ),
        ),
    )
    result = field_worklogs.submit(db_session, command)
    assert result[0].worklog.person_id is None
    assert field_worklogs.submit(db_session, command)[0].duplicate
    with pytest.raises(FieldAccessError, match="different details"):
        field_worklogs.submit(
            db_session,
            replace(command, entries=(replace(command.entries[0], notes="Changed"),)),
        )
    assert db_session.query(FieldWorkLog).one().author_vendor_user_id == membership_id
    assert db_session.query(FieldJobEvent).count() == 0


def test_vendor_timers_are_isolated_by_authenticated_user(db_session: Session) -> None:
    first, _, first_vendor = _vendor(db_session)
    second, _, second_vendor = _vendor(db_session)
    first_job, second_job = (
        _job(db_session, first_vendor),
        _job(db_session, second_vendor),
    )
    first_id, second_id = first.id, second.id
    first_public, second_public = first_job.public_id, second_job.public_id
    first_context, second_context = _context(first), _context(second)
    db_session.commit()
    field_transitions.apply(
        db_session,
        ApplyFieldTransition(
            first_context, first_id, first_public, FieldEvent.start, uuid4()
        ),
    )
    field_transitions.apply(
        db_session,
        ApplyFieldTransition(
            second_context, second_id, second_public, FieldEvent.start, uuid4()
        ),
    )
    field_transitions.apply(
        db_session,
        ApplyFieldTransition(
            first_context, first_id, first_public, FieldEvent.pause, uuid4()
        ),
    )
    logs = db_session.query(FieldWorkLog).all()
    assert len(logs) == 2
    assert (
        next(item for item in logs if item.system_user_id == first_id).end_at
        is not None
    )
    assert (
        next(item for item in logs if item.system_user_id == second_id).end_at is None
    )


def test_vendor_evidence_create_retry_and_completion(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    from uuid import UUID

    from app.models.stored_file import StoredFile
    from app.services.field.attachments import field_attachments, file_uploads
    from app.services.field.execution_contracts import CreateFieldAttachment

    def stage_upload(
        *,
        db: Session,
        domain: str,
        entity_type: str,
        entity_id: str,
        original_filename: str,
        content_type: str | None,
        data: bytes,
        uploaded_by: str | None,
        owner_subscriber_id: UUID | None = None,
    ) -> StoredFile:
        stored = StoredFile(
            entity_type=entity_type,
            entity_id=entity_id,
            original_filename=original_filename,
            content_type=content_type,
            storage_key_or_relative_path=f"unit-evidence/{uuid4()}",
            file_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
            storage_provider="s3",
        )
        db.add(stored)
        db.flush()
        return stored

    monkeypatch.setattr(file_uploads, "stage_upload", stage_upload)
    user, _, native = _vendor(db_session)
    job = _job(db_session, native)
    user_id, public_id = user.id, job.public_id
    context = _context(user)
    db_session.commit()
    transition = ApplyFieldTransition(
        context, user_id, public_id, FieldEvent.start, uuid4()
    )
    field_transitions.apply(db_session, transition)
    photo = CreateFieldAttachment(
        context=context,
        requester_system_user_id=user_id,
        public_id=public_id,
        kind="photo",
        file_name="site.png",
        mime_type="image/png",
        content=b"unit-image-evidence",
        client_ref=uuid4(),
    )
    saved = field_attachments.create(db_session, photo)
    assert saved.uploaded_by_person_id is None
    assert field_attachments.create(db_session, photo).id == saved.id
    with pytest.raises(FieldAccessError, match="different details"):
        field_attachments.create(
            db_session, replace(photo, signer_name="Changed metadata")
        )
    field_attachments.create(
        db_session,
        replace(
            photo,
            kind="signature",
            file_name="signature.png",
            client_ref=uuid4(),
            signer_name="Customer",
        ),
    )
    result = field_transitions.apply(
        db_session,
        replace(transition, event=FieldEvent.complete, client_event_id=uuid4()),
    )
    assert result.job.status == "completed"


def test_staff_geofence_calls_typed_transition_after_read_scope(
    db_session: Session,
) -> None:
    from app.models.domain_settings import DomainSetting, SettingDomain
    from app.services.field.geofence import GeofenceQuery, evaluate

    user = SystemUser(
        first_name="Staff", last_name="Crew", email=f"{uuid4()}@example.com"
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        system_user_id=user.id, person_id=user.id, crm_person_id=f"staff-{uuid4()}"
    )
    db_session.add(profile)
    db_session.flush()
    job = WorkOrder(
        public_id=f"wo-{uuid4()}",
        title="Staff geofence",
        status="scheduled",
        assigned_to_crm_person_id=profile.crm_person_id,
        metadata_={"latitude": 6.43, "longitude": 3.42},
    )
    db_session.add(job)
    db_session.add(
        DomainSetting(
            domain=SettingDomain.field,
            key="geofence_auto_status_enabled",
            value_text="true",
            is_active=True,
        )
    )
    user_id, public_id = user.id, job.public_id
    db_session.commit()
    fired = evaluate(db_session, GeofenceQuery(user_id, 6.43, 3.42))
    assert len(fired) == 1 and fired[0].public_id == public_id
    assert fired[0].event == FieldEvent.start
    assert evaluate(db_session, GeofenceQuery(user_id, 6.43, 3.42)) == ()


def test_vendor_principal_without_membership_cannot_use_stale_legacy_profile(
    db_session: Session,
) -> None:
    from app.models.subscriber import UserType
    from app.services.field.jobs import _profile_from_principal

    user, membership, _ = _vendor(db_session)
    user.user_type = UserType.vendor
    user_id = user.id
    db_session.add(
        TechnicianProfile(
            person_id=user_id, system_user_id=None, crm_person_id=f"stale-{uuid4()}"
        )
    )
    db_session.delete(membership)
    db_session.commit()
    with pytest.raises(FieldAccessError, match="membership"):
        resolve_field_actor(db_session, ResolveFieldActor(user_id))
    with pytest.raises(FieldAccessError, match="membership"):
        _profile_from_principal(db_session, {"principal_id": str(user_id)})
    with pytest.raises(FieldAccessError, match="membership"):
        field_jobs.list(db_session, FieldJobsQuery(user_id))


def test_actor_locks_native_before_only_current_bridge(db_session: Session) -> None:
    user, membership, native = _vendor(db_session)
    _, historical, _ = _vendor(db_session)
    db_session.add(
        FieldVendorUser(
            vendor_id=historical.vendor_id,
            system_user_id=user.id,
            role="field",
            is_active=False,
        )
    )
    user_id, native_id = user.id, native.id
    db_session.commit()
    locked_tables: list[str] = []

    def record_lock(execution) -> None:
        statement = execution.statement
        if getattr(statement, "_for_update_arg", None) is not None:
            locked_tables.extend(table.name for table in statement.get_final_froms())

    event.listen(db_session, "do_orm_execute", record_lock)
    try:
        actor = resolve_field_actor(db_session, ResolveFieldActor(user_id, lock=True))
    finally:
        event.remove(db_session, "do_orm_execute", record_lock)
    assert actor.native_vendor_id == native_id
    assert locked_tables == [
        SystemUser.__tablename__,
        FieldVendorUser.__tablename__,
        Vendor.__tablename__,
        FieldVendor.__tablename__,
    ]
