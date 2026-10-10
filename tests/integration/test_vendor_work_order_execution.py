"""Real migrated PostgreSQL proof for native vendor work-order execution."""

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app import config as app_config
from app.models.dispatch import WorkOrderAssignmentQueue
from app.models.field_attachment import FieldAttachment
from app.models.field_vendor import FieldVendor, FieldVendorUser
from app.models.field_worklog import FieldWorkLog
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.vendor_routes import Vendor
from app.models.work_order import WorkOrder
from app.services.field.attachments import field_attachments
from app.services.field.execution_contracts import (
    ApplyFieldTransition,
    CreateFieldAttachment,
    FieldAttachmentIdentity,
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
from app.services.field.transitions import field_transitions
from app.services.field.work_order_access import FieldAccessError
from app.services.field.worklogs import field_worklogs
from app.services.file_storage import UnifiedFileUploadService
from app.services.object_storage import StreamResult
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id
from app.services.work_order_assignment_contracts import (
    VendorAssignmentTarget,
    WorkOrderAssignmentCommand,
)
from app.services.work_order_commands import work_order_commands
from scripts.ci.migrated_test_database import install_migration_graph_environment
from scripts.ci.template_database import bootstrap_database_local_prerequisites


class _FakeUploads(UnifiedFileUploadService):
    """Keep object transport in memory while exercising real stored-file rows."""

    def __init__(self) -> None:
        self.contents: dict[str, bytes] = {}

    def stage_upload(
        self,
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
        record = StoredFile(
            entity_type=entity_type,
            entity_id=entity_id,
            original_filename=original_filename,
            storage_key_or_relative_path=f"attachments/{uuid4().hex}",
            file_size=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
            storage_provider="s3",
            uploaded_by=uploaded_by,
            owner_subscriber_id=owner_subscriber_id,
        )
        db.add(record)
        db.flush()
        db.refresh(record)
        self.contents[str(record.id)] = data
        return record

    def stream_file(self, file: StoredFile) -> StreamResult:
        data = self.contents[str(file.id)]
        return StreamResult(
            chunks=iter([data]),
            content_type=file.content_type,
            content_length=len(data),
        )


@pytest.fixture
def fresh_migration_database(
    template_base_url: URL, monkeypatch: pytest.MonkeyPatch
) -> Iterator[URL]:
    """Replay migration-path proofs from an empty, private PostgreSQL database."""
    name = f"dotmac_test_vendor_migration_{uuid4().hex}"
    maintenance = template_base_url.set(
        drivername="postgresql", database="postgres"
    ).render_as_string(hide_password=False)
    with psycopg.connect(maintenance, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    target = template_base_url.set(database=name)
    try:
        # Only database-local ACL prerequisites are bootstrapped. Alembic owns
        # every application table and composed module schema in this proof.
        bootstrap_database_local_prerequisites(target)
        install_migration_graph_environment()
        monkeypatch.setattr(
            app_config,
            "settings",
            replace(
                app_config.settings,
                database_url=target.render_as_string(hide_password=False),
            ),
        )
        yield target
    finally:
        with psycopg.connect(maintenance, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _context(actor="test-operator"):
    identity = uuid4()
    return CommandContext.system(
        actor=actor,
        scope="field:qa",
        reason="Labelled vendor work-order integration test",
        command_id=identity,
        idempotency_key=str(identity),
    )


def _subscriber(db):
    subscriber = Subscriber(
        first_name="QA",
        last_name="Customer",
        email=f"qa-{uuid4()}@dotmac.test",
        reseller_id=_default_reseller_id(db),
    )
    db.add(subscriber)
    db.flush()
    return subscriber.id


def _vendor(db):
    native = Vendor(name=f"QA vendor {uuid4()}")
    user = SystemUser(
        first_name="QA",
        last_name="Vendor",
        email=f"qa-{uuid4()}@dotmac.test",
        user_type=UserType.vendor,
    )
    db.add_all([native, user])
    db.flush()
    profile = FieldVendor(name=native.name, native_vendor_id=native.id)
    db.add(profile)
    db.flush()
    member = FieldVendorUser(vendor_id=profile.id, system_user_id=user.id, role="field")
    db.add(member)
    db.flush()
    return user.id, member.id, native.id


def test_vendor_journey_and_revoked_evidence_access(db_session, monkeypatch):
    from app.services.field import attachments as attachment_module

    uploads = _FakeUploads()
    monkeypatch.setattr(attachment_module, "file_uploads", uploads)
    user_id, member_id, vendor_id = _vendor(db_session)
    other_id, _, other_vendor_id = _vendor(db_session)
    job = WorkOrder(
        public_id=f"qa-vendor-{uuid4()}",
        title="QA vendor journey",
        subscriber_id=_subscriber(db_session),
        status="scheduled",
        scheduled_start=datetime.now(UTC),
    )
    db_session.add(job)
    db_session.flush()
    public_id = job.public_id
    job_id = job.id
    db_session.commit()
    assignment = WorkOrderAssignmentCommand(
        work_order_public_id=public_id, target=VendorAssignmentTarget(vendor_id)
    )
    work_order_commands.assign(db_session, command=assignment, context=_context())
    assert [j.id for j in field_jobs.list(db_session, FieldJobsQuery(user_id))] == [
        public_id
    ]
    with pytest.raises(FieldAccessError):
        field_jobs.get_detail(db_session, FieldJobQuery(other_id, public_id))
    db_session.rollback()
    start = ApplyFieldTransition(
        _context(str(user_id)), user_id, public_id, FieldEvent.start, uuid4()
    )
    assert field_transitions.apply(db_session, start).job.status == "in_progress"
    assert field_transitions.apply(db_session, start).replayed
    for event in (FieldEvent.pause, FieldEvent.resume):
        field_transitions.apply(
            db_session,
            replace(
                start,
                context=_context(str(user_id)),
                event=event,
                client_event_id=uuid4(),
            ),
        )
    note = CreateFieldWorkOrderNote(
        _context(str(user_id)), user_id, public_id, uuid4(), "QA completion note", True
    )
    assert create_field_work_order_note(db_session, note).author_person_id is None
    now = datetime.now(UTC)
    manual = SubmitFieldWorkLogs(
        _context(str(user_id)),
        user_id,
        public_id,
        (
            FieldWorkLogEntry(
                now - timedelta(hours=2),
                now - timedelta(hours=1),
                "QA manual time",
                uuid4(),
            ),
        ),
    )
    assert field_worklogs.submit(db_session, manual)[0].worklog.person_id is None
    assets = []
    for kind, name, content in (
        ("photo", "site.jpg", b"\xff\xd8\xff\xe0qa-photo"),
        ("signature", "signature.png", b"\x89PNG\r\n\x1a\nqa-signature"),
    ):
        upload = CreateFieldAttachment(
            context=_context(str(user_id)),
            requester_system_user_id=user_id,
            kind=kind,
            file_name=name,
            mime_type="image/jpeg" if kind == "photo" else "image/png",
            content=content,
            public_id=public_id,
            client_ref=uuid4(),
        )
        saved = field_attachments.create(db_session, upload)
        assets.append(saved)
        assert field_attachments.create(db_session, upload).id == saved.id
    assert (
        field_transitions.apply(
            db_session,
            replace(
                start,
                context=_context(str(user_id)),
                event=FieldEvent.complete,
                client_event_id=uuid4(),
            ),
        ).job.status
        == "completed"
    )
    assert {
        r.author_vendor_user_id
        for r in db_session.query(FieldWorkLog).filter_by(work_order_mirror_id=job_id)
    } == {member_id}
    assert (
        db_session.get(FieldAttachment, assets[0].id).uploaded_by_vendor_user_id
        == member_id
    )
    # Membership revocation blocks both reads and already-completed replay/download.
    db_session.get(FieldVendorUser, member_id).is_active = False
    db_session.commit()
    for operation in (
        lambda: field_transitions.apply(db_session, start),
        lambda: field_attachments.get_content(
            db_session, FieldAttachmentIdentity(user_id, assets[0].id)
        ),
    ):
        with pytest.raises(FieldAccessError):
            operation()
        db_session.rollback()
    assert other_vendor_id != vendor_id


def test_predecessor_upgrade_preserves_staff_assignment_and_enforces_vendor_xor(
    fresh_migration_database: URL,
):
    url = fresh_migration_database
    command.upgrade(Config("alembic.ini"), "667_captive_access_policy_backfill")
    engine = create_engine(url)
    tech_id, person_id, job_id, queue_id = (uuid4() for _ in range(4))
    with Session(engine) as db:
        subscriber_id = _subscriber(db)
        db.commit()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO technician_profiles(id,person_id,is_active,created_at,updated_at) VALUES (:id,:person,true,now(),now())"
            ),
            {"id": tech_id, "person": person_id},
        )
        conn.execute(
            text(
                "INSERT INTO work_order(id,public_id,subscriber_id,title,status,is_active,created_at,updated_at) VALUES (:id,:public,:subscriber,'QA pre-upgrade','dispatched',true,now(),now())"
            ),
            {"id": job_id, "public": f"qa-pre-{uuid4()}", "subscriber": subscriber_id},
        )
        conn.execute(
            text(
                "INSERT INTO work_order_assignment_queue(id,work_order_mirror_id,status,assigned_technician_id,created_at,updated_at) VALUES (:id,:job,'assigned',:tech,now(),now())"
            ),
            {"id": queue_id, "job": job_id, "tech": tech_id},
        )
    command.upgrade(Config("alembic.ini"), "669_native_vendor_work_order_assignment")
    with engine.connect() as conn:
        assert conn.execute(
            text(
                "SELECT assigned_technician_id,assigned_vendor_id FROM work_order_assignment_queue WHERE id=:id"
            ),
            {"id": queue_id},
        ).one() == (tech_id, None)
    indexes = {
        i["name"] for i in inspect(engine).get_indexes("work_order_assignment_queue")
    }
    assert "uq_work_order_current_assignment" in indexes
    with (
        pytest.raises(IntegrityError, match="ck_work_order_assignment_target"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO work_order_assignment_queue(id,work_order_mirror_id,status,created_at,updated_at) VALUES (:id,:job,'assigned',now(),now())"
            ),
            {"id": uuid4(), "job": job_id},
        )
    with (
        pytest.raises(IntegrityError, match="uq_work_order_current_assignment"),
        engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO work_order_assignment_queue(id,work_order_mirror_id,status,assigned_technician_id,created_at,updated_at) VALUES (:id,:job,'assigned',:tech,now(),now())"
            ),
            {"id": uuid4(), "job": job_id, "tech": tech_id},
        )
    engine.dispose()


def test_competing_assignments_keep_exactly_one_current_target(cloned_database):
    url = cloned_database("heads")
    engine = create_engine(url)
    with Session(engine, expire_on_commit=False) as db:
        _, _, vendor_one = _vendor(db)
        _, _, vendor_two = _vendor(db)
        job = WorkOrder(
            public_id=f"qa-race-{uuid4()}",
            title="QA concurrent dispatch",
            subscriber_id=_subscriber(db),
            status="scheduled",
        )
        db.add(job)
        db.flush()
        public_id = job.public_id
        job_id = job.id
        db.commit()
    barrier = Barrier(2)

    def assign(vendor_id):
        with Session(engine) as db:
            db.execute(text("SET statement_timeout='15s'"))
            db.commit()
            barrier.wait(timeout=10)
            return work_order_commands.assign(
                db,
                command=WorkOrderAssignmentCommand(
                    work_order_public_id=public_id,
                    target=VendorAssignmentTarget(vendor_id),
                ),
                context=_context(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(assign, (vendor_one, vendor_two)))
    with Session(engine) as db:
        current = (
            db.query(WorkOrderAssignmentQueue)
            .filter_by(work_order_mirror_id=job_id, status="assigned")
            .all()
        )
        assert len(current) == 1 and current[0].assigned_vendor_id in {
            vendor_one,
            vendor_two,
        }
        assert len(outcomes) == 2
        assert {o.queue_id for o in outcomes} == {current[0].id}
    engine.dispose()


def _concurrent(engine, *operations):
    barrier = Barrier(len(operations))

    def run(operation):
        with Session(engine, expire_on_commit=False) as db:
            db.execute(text("SET statement_timeout='15s'"))
            db.execute(text("SET lock_timeout='10s'"))
            db.commit()
            barrier.wait(timeout=10)
            return operation(db)

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        return list(pool.map(run, operations))


def test_concurrent_members_with_reversed_history_and_shared_vendor(cloned_database):
    from app.services.field.execution_contracts import UpdateFieldJobLocation

    url = cloned_database("heads")
    engine = create_engine(url)
    with Session(engine, expire_on_commit=False) as db:
        user_one, member_one, vendor_one = _vendor(db)
        user_two, member_two, vendor_two = _vendor(db)
        first = db.get(FieldVendorUser, member_one)
        second = db.get(FieldVendorUser, member_two)
        db.add_all(
            [
                FieldVendorUser(
                    vendor_id=second.vendor_id,
                    system_user_id=user_one,
                    role="field",
                    is_active=False,
                ),
                FieldVendorUser(
                    vendor_id=first.vendor_id,
                    system_user_id=user_two,
                    role="field",
                    is_active=False,
                ),
            ]
        )
        jobs = [
            WorkOrder(
                public_id=f"qa-history-{uuid4()}",
                title="QA history locks",
                subscriber_id=_subscriber(db),
                status="dispatched",
            )
            for _ in range(2)
        ]
        db.add_all(jobs)
        db.flush()
        db.add_all(
            [
                WorkOrderAssignmentQueue(
                    work_order_mirror_id=jobs[0].id,
                    assigned_vendor_id=vendor_one,
                    status="assigned",
                ),
                WorkOrderAssignmentQueue(
                    work_order_mirror_id=jobs[1].id,
                    assigned_vendor_id=vendor_two,
                    status="assigned",
                ),
            ]
        )
        public_one, public_two = jobs[0].public_id, jobs[1].public_id
        third = SystemUser(
            first_name="QA",
            last_name="Second member",
            email=f"qa-{uuid4()}@dotmac.test",
            user_type=UserType.vendor,
        )
        db.add(third)
        db.flush()
        third_id = third.id
        db.add(
            FieldVendorUser(
                vendor_id=first.vendor_id, system_user_id=third_id, role="field"
            )
        )
        db.commit()
    results = _concurrent(
        engine,
        lambda db: field_transitions.apply(
            db,
            ApplyFieldTransition(
                _context(str(user_one)), user_one, public_one, FieldEvent.start, uuid4()
            ),
        ),
        lambda db: field_transitions.apply(
            db,
            ApplyFieldTransition(
                _context(str(user_two)), user_two, public_two, FieldEvent.start, uuid4()
            ),
        ),
    )
    assert all(result.job.status == "in_progress" for result in results)
    results = _concurrent(
        engine,
        lambda db: field_transitions.apply(
            db,
            ApplyFieldTransition(
                _context(str(user_one)), user_one, public_one, FieldEvent.pause, uuid4()
            ),
        ),
        lambda db: field_jobs.update_location(
            db,
            UpdateFieldJobLocation(
                _context(str(third_id)), third_id, public_one, 9.0765, 7.3986
            ),
        ),
    )
    assert results[0].job.status == "paused"
    assert results[1].latitude == 9.0765
    engine.dispose()


def test_public_revocation_races_execution_and_closes_replay(cloned_database):
    from app.services import vendor_user_provisioning

    url = cloned_database("heads")
    engine = create_engine(url)
    with Session(engine, expire_on_commit=False) as db:
        user_id, member_id, vendor_id = _vendor(db)
        job = WorkOrder(
            public_id=f"qa-revoke-{uuid4()}",
            title="QA revocation race",
            subscriber_id=_subscriber(db),
            status="dispatched",
        )
        db.add(job)
        db.flush()
        public_id = job.public_id
        db.add(
            WorkOrderAssignmentQueue(
                work_order_mirror_id=job.id,
                assigned_vendor_id=vendor_id,
                status="assigned",
            )
        )
        db.commit()
    transition = ApplyFieldTransition(
        _context(str(user_id)), user_id, public_id, FieldEvent.start, uuid4()
    )

    def execute(db):
        try:
            return field_transitions.apply(db, transition)
        except FieldAccessError:
            return "revoked"

    def revoke(db):
        return vendor_user_provisioning.revoke_committed(
            db, member_id, context=_context()
        )

    result, _ = _concurrent(engine, execute, revoke)
    assert result == "revoked" or result.job.status == "in_progress"
    with Session(engine) as db:
        assert not db.get(FieldVendorUser, member_id).is_active
        assert not db.get(SystemUser, user_id).is_active
        db.rollback()
        with pytest.raises(FieldAccessError):
            field_transitions.apply(db, transition)
    engine.dispose()
