"""Non-authoritative fast unit lane for assigned-job vendor map disclosure."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.field import router
from app.db import get_db
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.field_vendor import FieldVendorUser
from app.models.network import FdhCabinet
from app.models.project import Project, ProjectInfrastructure
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.field.execution_contracts import FieldJobQuery
from app.services.field.job_brief import FieldJobBriefQuery, get_job_brief
from app.services.field.jobs import field_jobs
from app.services.field.work_order_access import FieldAccessError
from app.services.work_order_commands import work_order_commands
from tests.test_vendor_attending_person import Fixture, command, context
from tests.test_vendor_attending_person import visit as selected_visit_fixture

visit = selected_visit_fixture


@pytest.fixture
def mapped_visit(db_session: Session, visit: Fixture) -> Fixture:
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


def _query(visit: Fixture, user: UUID | None = None) -> FieldJobQuery:
    return FieldJobQuery(user or visit.first_user, visit.public_id)


def _client(db: Session, user: UUID) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_id": str(user),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }
    return TestClient(app)


def test_vendor_job_site_excludes_even_collocated_unrelated_assets(
    db_session: Session, mapped_visit: Fixture
) -> None:
    items = field_jobs.list_destinations(db_session, _query(mapped_visit))
    assert [item.destination_type for item in items] == ["customer"]
    assert (items[0].latitude, items[0].longitude) == (9.071, 7.451)
    assert "Unrelated near job" not in {item.label for item in items}


def test_explicit_current_project_target_is_visible_without_adjacent_inventory(
    db_session: Session, mapped_visit: Fixture
) -> None:
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
    job.project_id = project.id
    target_id = target.id
    db_session.commit()
    items = field_jobs.list_destinations(db_session, _query(mapped_visit))
    assert [(item.destination_type, item.label) for item in items] == [
        ("customer", "Job site"),
        ("project_target", "Explicit target"),
    ]
    assert items[1].destination_id == str(target_id)
    assert (items[1].latitude, items[1].longitude) == (9.072, 7.452)


@pytest.mark.parametrize(
    "fault",
    ["other_member", "other_job", "revoked_member", "reassigned", "missing_generation"],
)
def test_unrelated_and_revoked_job_location_reads_fail_closed(
    db_session: Session, mapped_visit: Fixture, fault: str
) -> None:
    query = _query(mapped_visit)
    if fault == "other_member":
        query = _query(mapped_visit, mapped_visit.second_user)
    elif fault == "other_job":
        query = replace(query, public_id="unrelated-job")
    elif fault == "revoked_member":
        db_session.get(FieldVendorUser, mapped_visit.first_member).is_active = False
    else:
        queue = db_session.scalar(
            select(WorkOrderAssignmentQueue).where(
                WorkOrderAssignmentQueue.assigned_vendor_user_id
                == mapped_visit.first_member
            )
        )
        if fault == "missing_generation":
            queue.assignment_generation = None
        else:
            queue.assigned_vendor_user_id = mapped_visit.second_member
    db_session.commit()
    with pytest.raises(FieldAccessError):
        field_jobs.list_destinations(db_session, query)
    with pytest.raises(FieldAccessError):
        get_job_brief(
            db_session,
            FieldJobBriefQuery(query.requester_system_user_id, query.public_id),
        )


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/map-assets?asset_type=fdh_cabinet", None),
        ("get", "/map-assets/nearby?latitude=9.071&longitude=7.451", None),
        ("get", "/map-assets/search?q=near", None),
        ("get", "/vendor/map-assets/nearby?lat=9.071&lng=7.451", None),
        ("get", "/vendor/map-assets/search?q=near", None),
        (
            "patch",
            "/map-assets/fdh_cabinet/{asset}/location",
            {"latitude": 9, "longitude": 7},
        ),
        ("post", "/map-assets/fdh_cabinet/{asset}/revert-location", None),
    ],
)
def test_vendor_cannot_bypass_job_map_via_global_api_even_with_stale_staff_profile(
    db_session: Session,
    mapped_visit: Fixture,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None,
) -> None:
    # Any vendor membership prohibits a stale profile from entering staff plane.
    db_session.add(
        TechnicianProfile(
            system_user_id=mapped_visit.first_user,
            person_id=mapped_visit.first_user,
            is_active=True,
        )
    )
    asset = db_session.scalar(select(FdhCabinet))
    asset_id = asset.id
    db_session.commit()
    client = _client(db_session, mapped_visit.first_user)
    response = client.request(
        method=method, url="/api/v1/field" + path.format(asset=asset_id), json=body
    )
    assert response.status_code == 403
    db_session.refresh(asset)
    assert float(asset.latitude) == 9.071


def test_vendor_job_destination_api_still_delivers_navigation_target(
    db_session: Session, mapped_visit: Fixture
) -> None:
    response = _client(db_session, mapped_visit.first_user).get(
        f"/api/v1/field/jobs/{mapped_visit.public_id}/destinations"
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["latitude"] == 9.071
    assert response.json()["count"] == 1
