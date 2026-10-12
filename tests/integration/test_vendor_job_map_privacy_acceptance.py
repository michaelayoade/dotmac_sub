"""Assigned-job map privacy on real migrated PostgreSQL; no request adapters."""

from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.network import FdhCabinet
from app.models.project import Project, ProjectInfrastructure
from app.models.work_order import WorkOrder
from app.services.field.execution_contracts import FieldJobQuery
from app.services.field.job_brief import FieldJobBriefQuery, get_job_brief
from app.services.field.jobs import field_jobs
from app.services.field.work_order_access import (
    FieldAccessError,
    ResolveFieldActor,
    require_technician_map_actor,
)
from app.services.work_order_commands import work_order_commands
from scripts.ci.migrated_test_database import require_migrated_schema
from tests.integration.test_vendor_attending_person_acceptance import (
    db_session as migrated_session_fixture,
)
from tests.integration.test_vendor_attending_person_acceptance import (
    engine as migrated_engine_fixture,
)
from tests.test_vendor_attending_person import Fixture, command, context
from tests.test_vendor_attending_person import visit as selected_visit_fixture

engine = migrated_engine_fixture
db_session = migrated_session_fixture
visit = selected_visit_fixture


@pytest.fixture
def mapped_visit(db_session: Session, visit: Fixture) -> Fixture:
    bind = db_session.get_bind()
    assert isinstance(bind, Engine)
    require_migrated_schema(bind)
    db_session.commit()
    work_order_commands.assign(
        db_session, command=command(visit), context=context(visit)
    )
    job = db_session.scalar(
        select(WorkOrder).where(WorkOrder.public_id == visit.public_id)
    )
    assert job is not None
    job.metadata_ = {"location": {"lat": 9.071, "lng": 7.451}}
    db_session.add(
        FdhCabinet(
            name="Unrelated near job",
            code=f"near-{uuid4()}",
            latitude=9.071,
            longitude=7.451,
            is_active=True,
        )
    )
    db_session.commit()
    return visit


def test_pg_vendor_destinations_are_explicit(
    db_session: Session, mapped_visit: Fixture
) -> None:
    query = FieldJobQuery(mapped_visit.first_user, mapped_visit.public_id)
    items = field_jobs.list_destinations(db_session, query)
    assert [(item.destination_type, item.label) for item in items] == [
        ("customer", "Job site")
    ]
    assert (items[0].latitude, items[0].longitude) == (9.071, 7.451)
    target = FdhCabinet(
        name="Explicit target",
        code=f"target-{uuid4()}",
        latitude=9.072,
        longitude=7.452,
        is_active=True,
    )
    project = Project(name="Native mapped project", is_active=True)
    db_session.add_all([target, project])
    db_session.flush()
    db_session.add(ProjectInfrastructure(project_id=project.id, cabinet_id=target.id))
    job = db_session.scalar(
        select(WorkOrder).where(WorkOrder.public_id == mapped_visit.public_id)
    )
    assert job is not None
    job.project_id = project.id
    target_id = target.id
    db_session.commit()
    items = field_jobs.list_destinations(db_session, query)
    assert [(item.destination_type, item.label) for item in items] == [
        ("customer", "Job site"),
        ("project_target", "Explicit target"),
    ]
    assert items[1].destination_id == str(target_id)
    assert (items[1].latitude, items[1].longitude) == (9.072, 7.452)


def test_pg_vendor_reassignment_revokes_map_read(
    db_session: Session, mapped_visit: Fixture
) -> None:
    queue = db_session.scalar(
        select(WorkOrderAssignmentQueue).where(
            WorkOrderAssignmentQueue.assigned_vendor_user_id
            == mapped_visit.first_member
        )
    )
    assert queue is not None
    queue.assigned_vendor_user_id = mapped_visit.second_member
    db_session.commit()
    with pytest.raises(FieldAccessError):
        field_jobs.list_destinations(
            db_session, FieldJobQuery(mapped_visit.first_user, mapped_visit.public_id)
        )
    with pytest.raises(FieldAccessError):
        get_job_brief(
            db_session,
            FieldJobBriefQuery(mapped_visit.first_user, mapped_visit.public_id),
        )


def test_pg_vendor_global_map_admission_is_refused_with_stale_staff_profile(
    db_session: Session, mapped_visit: Fixture
) -> None:
    db_session.add(
        TechnicianProfile(
            system_user_id=mapped_visit.first_user,
            person_id=mapped_visit.first_user,
            is_active=True,
        )
    )
    db_session.commit()
    with pytest.raises(FieldAccessError):
        require_technician_map_actor(
            db_session, ResolveFieldActor(mapped_visit.first_user)
        )
