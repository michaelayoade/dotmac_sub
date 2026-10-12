"""Fast, non-authoritative unit evidence for assignment-scoped job briefs."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.field.job_brief import router
from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
)
from app.models.dispatch import TechnicianProfile, WorkOrderAssignmentQueue
from app.models.network import FdhCabinet
from app.models.network_monitoring import (
    OutageIncident,
    OutageIncidentWorkOrderLink,
    OutageScopeRevision,
    OutageScopeRevisionMember,
)
from app.models.project import Project, ProjectTask
from app.models.subscriber import Address, Subscriber
from app.models.support import Ticket
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.auth_dependencies import require_user_auth
from app.services.bulk_actions import membership_scope_token
from app.services.field.job_brief import FieldJobBriefQuery, get_job_brief
from app.services.field.work_order_access import FieldAccessError
from app.services.topology.outage_audience_read import (
    AudienceKnowledge,
    AudienceScope,
    OutageAudienceQuery,
    OutageAudienceReadError,
    read_outage_audience,
)


def _actor(db: Session) -> SystemUser:
    user = SystemUser(
        first_name="Field", last_name="Crew", email=f"{uuid4()}@example.com"
    )
    db.add(user)
    db.flush()
    profile = TechnicianProfile(system_user_id=user.id, person_id=user.id)
    db.add(profile)
    db.flush()
    return user


def _revision(
    db: Session,
    incident: OutageIncident,
    target: UUID | None,
    ids: tuple[UUID, ...],
    *,
    sequence: int = 1,
    scope_type: str = "fdh-cabinet",
) -> OutageScopeRevision:
    token = membership_scope_token(
        f"{scope_type}:{target}", [str(value) for value in ids]
    )
    revision = OutageScopeRevision(
        incident_id=incident.id,
        sequence=sequence,
        effective_at=datetime.now(UTC),
        reason="confirmed",
        new_scope_type=scope_type,
        new_scope_id=target,
        membership_token=token,
        member_count=len(ids),
        entered_count=1 if ids else 0,
        left_count=0,
    )
    db.add(revision)
    db.flush()
    db.add_all(
        [
            OutageScopeRevisionMember(
                revision_id=revision.id,
                subscription_id=sid,
                membership="entered" if i == 0 else "retained",
            )
            for i, sid in enumerate(ids)
        ]
    )
    db.flush()
    return revision


def _seed(
    db: Session,
) -> tuple[SystemUser, WorkOrder, OutageIncidentWorkOrderLink, tuple[UUID, ...]]:
    user = _actor(db)
    profile = db.query(TechnicianProfile).filter_by(system_user_id=user.id).one()
    cabinet = FdhCabinet(
        name="Shared FDH", code=f"FDH-{uuid4()}", latitude=9.1, longitude=7.4
    )
    offer = CatalogOffer(
        name="Field fixture",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    ticket = Ticket(
        title="Shared feeder fault", description="Restricted internal investigation"
    )
    project = Project(name="Fiber rollout")
    db.add_all([cabinet, offer, ticket, project])
    db.flush()
    task = ProjectTask(
        project_id=project.id,
        title="Repair feeder",
        description="Replace damaged section",
        status="in_progress",
        template_plan_state="current",
    )
    db.add(task)
    db.flush()
    job = WorkOrder(
        public_id=f"brief-{uuid4()}",
        title="Feeder recovery",
        description="Inspect and repair feeder",
        work_order_kind="infrastructure",
        origin_ticket_id=ticket.id,
        project_id=project.id,
        project_task_id=task.id,
        address="Shared cabinet access road",
        metadata_={"latitude": 999, "longitude": 7.4},
    )
    incident = OutageIncident(fdh_cabinet_id=cabinet.id, note="Shared feeder fault")
    db.add_all([job, incident])
    db.flush()
    db.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=job.id,
            assigned_technician_id=profile.id,
            status="assigned",
        )
    )
    ids = (UUID(int=1), UUID(int=2), UUID(int=3))
    for i, sid in enumerate(ids[:2]):
        customer = Subscriber(
            first_name=f"Customer {i}",
            last_name="Field",
            email=f"{uuid4()}@example.com",
        )
        db.add(customer)
        db.flush()
        db.add(Subscription(id=sid, subscriber_id=customer.id, offer_id=offer.id))
    # Third subscription has already been deleted; revision membership outlives it.
    revision = _revision(db, incident, cabinet.id, ids)
    db.add(
        OutageScopeRevisionMember(
            revision_id=revision.id, subscription_id=UUID(int=4), membership="left"
        )
    )
    revision.left_count = 1
    link = OutageIncidentWorkOrderLink(
        incident_id=incident.id,
        work_order_id=job.id,
        scope_revision_id=revision.id,
        scope_revision_sequence=revision.sequence,
        membership_token=revision.membership_token,
        target_type="fdh-cabinet",
        target_id=cabinet.id,
        idempotency_key=str(uuid4()),
        command_fingerprint="unit-fixture",
    )
    db.add(link)
    db.flush()
    return user, job, link, ids


def test_brief_has_multiple_customers_task_signoff_and_exact_asset(
    db_session: Session,
) -> None:
    user, job, link, ids = _seed(db_session)
    brief = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    assert brief.issue == "Shared feeder fault"
    assert brief.instructions == "Inspect and repair feeder"
    assert brief.affected_subscription_count == 3
    assert tuple(item.subscription_id for item in brief.affected_customers) == ids
    assert brief.affected_customers[2].details_available is False
    assert brief.tasks[0].title == "Repair feeder"
    assert brief.tasks[0].status == "in_progress"
    assert brief.tasks[0].completed_at is None
    assert brief.signoff.requires_as_built_evidence is True
    assert brief.directions[0].latitude is None  # invalid pin is never sent
    assert brief.directions[0].address_text == "Shared cabinet access road"
    assert brief.directions[1].latitude == 9.1
    assert brief.outage_audience is not None
    assert brief.outage_audience.issued.id == link.scope_revision_id


def test_other_job_and_inactive_actor_cannot_read_audience(db_session: Session) -> None:
    user, job, link, _ = _seed(db_session)
    stranger = _actor(db_session)
    with pytest.raises(FieldAccessError):
        get_job_brief(db_session, FieldJobBriefQuery(stranger.id, job.public_id))
    with pytest.raises(FieldAccessError):
        read_outage_audience(
            db_session, OutageAudienceQuery(stranger.id, job.public_id, link.id)
        )
    user.is_active = False
    db_session.flush()
    with pytest.raises(FieldAccessError):
        get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))


def test_link_from_another_work_order_is_refused(db_session: Session) -> None:
    user, job, link, _ = _seed(db_session)
    other = WorkOrder(public_id=f"other-{uuid4()}", title="Other recovery")
    db_session.add(other)
    db_session.flush()
    link.work_order_id = other.id
    db_session.flush()
    with pytest.raises(OutageAudienceReadError, match="not found"):
        read_outage_audience(
            db_session, OutageAudienceQuery(user.id, job.public_id, link.id)
        )


def test_pages_pin_revision_and_keep_issued_and_current_distinct(
    db_session: Session,
) -> None:
    user, job, link, ids = _seed(db_session)
    query = OutageAudienceQuery(user.id, job.public_id, link.id, limit=1)
    first = read_outage_audience(db_session, query)
    assert first.subscription_ids == (ids[0],)
    assert first.current is not None
    second = read_outage_audience(
        db_session,
        replace(
            query,
            expected_revision_id=first.current.id,
            after_subscription_id=first.next_after_subscription_id,
        ),
    )
    assert second.subscription_ids == (ids[1],)
    incident = db_session.get(OutageIncident, link.incident_id)
    assert incident is not None
    newest = _revision(db_session, incident, link.target_id, (ids[1],), sequence=2)
    with pytest.raises(OutageAudienceReadError, match="changed"):
        read_outage_audience(
            db_session,
            replace(
                query,
                expected_revision_id=first.current.id,
                after_subscription_id=ids[0],
            ),
        )
    current = read_outage_audience(db_session, query)
    assert current.current is not None and current.current.id == newest.id
    assert current.issued.id == first.issued.id
    issued = read_outage_audience(
        db_session, replace(query, scope=AudienceScope.issued, limit=100)
    )
    assert issued.subscription_ids == ids


def test_unknown_scope_is_not_zero_customers(db_session: Session) -> None:
    user, job, link, _ = _seed(db_session)
    incident = db_session.get(OutageIncident, link.incident_id)
    assert incident is not None
    _revision(db_session, incident, None, (), sequence=2, scope_type="unresolved")
    brief = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    assert brief.affected_subscription_count is None
    assert brief.affected_customers == ()
    assert brief.outage_audience is not None
    assert brief.outage_audience.knowledge == AudienceKnowledge.unknown
    assert brief.outage_audience.issued.subscription_count == 3


def test_corrupt_membership_token_is_refused(db_session: Session) -> None:
    user, job, link, _ = _seed(db_session)
    revision = db_session.get(OutageScopeRevision, link.scope_revision_id)
    assert revision is not None
    revision.member_count = 0
    db_session.flush()
    with pytest.raises(OutageAudienceReadError, match="inconsistent"):
        get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))


def test_missing_issued_revision_fails_closed(db_session: Session) -> None:
    user, job, link, _ = _seed(db_session)
    # A missing immutable record must fail closed if persistence is unavailable;
    # do not defeat the real FK just to manufacture corrupt database history.
    with patch.object(db_session, "get", return_value=None):
        with pytest.raises(OutageAudienceReadError, match="unavailable"):
            get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))


def test_http_brief_serializes_allowlist_and_maps_access_errors(
    db_session: Session,
) -> None:
    user, job, _, _ = _seed(db_session)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_type": "system_user",
        "principal_id": str(user.id),
    }
    client = TestClient(app)
    response = client.get(f"/jobs/{job.public_id}/brief")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["affected_customers"]) == 3
    assert set(payload["affected_customers"][0]) == {
        "subscription_id",
        "subscriber_id",
        "name",
        "service_address",
        "details_available",
        "details_provenance",
    }
    assert "Restricted internal investigation" not in response.text
    stranger = _actor(db_session)
    app.dependency_overrides[require_user_auth] = lambda: {
        "principal_type": "system_user",
        "principal_id": str(stranger.id),
    }
    assert client.get(f"/jobs/{job.public_id}/brief").status_code == 404


def test_shared_flutter_fixture_matches_actual_brief_contract() -> None:
    from app.services.field.job_brief import FieldJobBrief

    path = (
        Path(__file__).resolve().parents[1]
        / "field_mobile/test/fixtures/job_brief.json"
    )
    brief = TypeAdapter(FieldJobBrief).validate_json(path.read_text())
    assert brief.affected_subscription_count == 2
    assert brief.outage_audience is not None
    assert brief.outage_audience.issued.sequence == 1
    assert brief.outage_audience.current is not None
    assert brief.outage_audience.current.sequence == 2


@pytest.mark.parametrize("technology", ["fiber", "radio"])
def test_explicit_rollout_recommends_planning_without_claiming_completion(
    db_session: Session, technology: str
) -> None:
    user, job, _, _ = _seed(db_session)
    job.purpose = "infrastructure_rollout"
    job.access_technology = technology
    job.classification_source = "explicit_operator_selection"
    job.template_version = 1
    db_session.flush()
    brief = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    assert brief.template_recommendation is not None
    assert brief.template_recommendation.use.value == "recommendation_only"
    assert brief.template_recommendation.identity.access_technology.value == technology
    assert brief.tasks[0].completed_at is None
    newer = replace(
        brief.template_recommendation,
        identity=replace(brief.template_recommendation.identity, version=2),
    )
    with patch("app.services.field.job_brief.recommend_template", return_value=newer):
        stale = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    assert stale.template_recommendation is None
    assert stale.template_recommendation_status.value == "unavailable_stale_template"
    assert stale.classification is not None


def test_brief_omits_service_address_owned_by_another_customer(
    db_session: Session,
) -> None:
    user, job, _, ids = _seed(db_session)
    outsider = Subscriber(
        first_name="Outside", last_name="Audience", email=f"{uuid4()}@example.com"
    )
    db_session.add(outsider)
    db_session.flush()
    address = Address(
        subscriber_id=outsider.id, address_line1="Synthetic unrelated address"
    )
    db_session.add(address)
    db_session.flush()
    subscription = db_session.get(Subscription, ids[0])
    assert subscription is not None
    subscription.service_address_id = address.id
    db_session.flush()

    brief = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    customer = brief.affected_customers[0]
    assert customer.subscription_id == ids[0]
    assert customer.subscriber_id == subscription.subscriber_id
    assert customer.details_available is True
    assert customer.service_address is None
    assert brief.affected_subscription_count == 3

    # A legitimate address remains visible without widening audience membership.
    address.subscriber_id = subscription.subscriber_id
    db_session.flush()
    updated = get_job_brief(db_session, FieldJobBriefQuery(user.id, job.public_id))
    assert (
        updated.affected_customers[0].service_address == "Synthetic unrelated address"
    )
